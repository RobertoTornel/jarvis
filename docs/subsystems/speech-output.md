# Speech output and TTS

**Authoritative spec:** [`src/claire/output/tts.spec.md`](../../src/claire/output/tts.spec.md).
Read this when you are changing how a reply becomes audio, adding an engine,
tuning barge-in, or debugging silence.

`tts.spec.md` states the three engines and which synthesis happens locally. The
default engine is **Fish Audio**, a cloud streaming synthesiser.

## Responsibility

Turning the reply text into sound and stopping that sound the moment the user
interrupts. Three engines sit behind one interface
(`start`/`stop`/`speak`/`interrupt`/`is_speaking`/`get_last_spoken_text`, plus
`enabled`):

| Engine | Implementation | Where synthesis happens |
|---|---|---|
| `fish` (default) | `src/claire/output/fish_tts.py:634` `FishTTS` | Cloud: `POST https://api.fish.audio/v1/tts` (`fish_client.py:68`), sentence-pipelined into a local streaming player that verifies the bytes are PCM it can render. |
| `piper` | `src/claire/output/tts.py:666` `PiperTTS` | Local ONNX voice; missing voice files are downloaded from the configured model source. |
| `chatterbox` | `src/claire/output/tts.py:400` `ChatterboxTTS` | Local model weights (requires PyTorch). |

`SUPPORTED_TTS_ENGINES = ("piper", "chatterbox", "fish")`
(`src/claire/output/tts.py:1038`). The factory is `create_tts_engine`
(`tts.py:1063`). An unrecognised engine name raises `ValueError` (`tts.py:1154`)
rather than substituting a different voice.

`FishTTS` is exposed lazily through the module's `__getattr__` (`tts.py:1048`)
so that importing `claire.output.tts` never reads a key file. Anything that
probes `hasattr(tts_module, "FishTTS")` still sees it.

## The streaming synth and play path (Fish)

`FishTTS` (`fish_tts.py:634`) is a two-thread pipeline around
`fish_client.FishClient`:

```
speak(text)  ──► sentence queue ──► _synth_loop (fish_tts.py:936)
                    │                   │  one _synth_utterance per sentence (:966)
                    │                   ▼
                    │            FishClient.stream_audio  ──► POST api.fish.audio
                    │                   │
                    │                   ▼
                    └────────────► _play_q ──► _play_loop (:1056)
                                                   │  ordered release, byte ceiling
                                                   ▼
                                          _SoundDevicePlayer (:509)
                                          RawOutputStream.write  ──► speaker
```

Five properties are worth knowing before you change it:

1. **Sentence pipelining.** The reply is split by `_split_sentences`
   (`fish_tts.py:453`) so synthesis of sentence N+1 overlaps playback of
   sentence N. Without it the round trip is audible as a gap at every full stop.
2. **Bounded buffering.** Audio is released in order through one queue with a
   byte ceiling, so a long reply is never assembled entirely in memory before it
   starts speaking.
3. **Declared sample rate, never a guess.** `_PcmStreamFormat`
   (`fish_tts.py:294`) sniffs the first bytes: a RIFF/WAV header gives the rate,
   headerless PCM falls back to what was requested (`_coerce_sample_rate`,
   `:183`). Fish does not support 48000; playing a 44100 stream at 48000 comes
   out pitched down.
4. **Only PCM it can render is played.** The player writes signed 16-bit mono,
   so the format layer must refuse anything else rather than reinterpret it.
   An MP3 body or an unparsable container, read as int16, is a full-scale burst
   rather than speech; `FishUnsupportedAudioFormatError`
   (`fish_client.py:167`) is raised for that sentence instead, and a buffer that
   is not a whole number of frames is reported rather than silently dropped.
   `MAX_WAV_HEADER_BYTES` (`fish_tts.py:92`), `PLAYBACK_FRAME_BYTES` (`:110`)
   and `_limit_peak` (`:600`) are the named constants of that contract.
5. **Barge-in in tens of milliseconds.** `flush` (`fish_tts.py:827`) bumps the
   generation, clears `_is_speaking`, drops the queued audio, releases any
   producer blocked on the byte ceiling, and aborts the device instead of
   waiting for the current sentence. `interrupt` (`:817`) is a thin alias for
   `flush`, so it is safe to call when idle. The Piper and Chatterbox engines do
   not implement `flush`; the listener calls `interrupt`.
   `tests/test_fish_tts.py` pins this behaviour, and
   `tests/test_fish_tts_loud_burst.py` pins the format gate.

Wire-level rules live in `fish_client.py:1`–`:48`: the call is
`POST https://api.fish.audio/v1/tts` with a Bearer key and the model id in a
`model` header. HTTP status decides the key's fate:

| Status | Meaning | Action |
|---|---|---|
| 401, 402, 403, 429 | dead key | rotate to the next key; voice id untouched |
| 400, 404, 405, 409, 413, 422 | bad request | raise immediately; do not burn other keys |
| anything else | transient | retry once on the same key, then raise |

A key value is a secret: it is used for the `Authorization` header and for a
fingerprint (last four characters plus a hash) in messages, never logged.

## The Piper and Chatterbox path

`PiperTTS.speak` (`tts.py:825`) queues text to a worker thread (`_run`,
`tts.py:854`; `_speak_once`, `:869`). Piper is the only engine that reports an
exact duration, which is why the listener's `duration_callback` closure in
`VoiceListener._dispatch_query` is annotated "(Piper only)" and why
`_estimate_tts_duration` (`tts.py:203`) exists for engines that do not.
Markdown is stripped before speech by `_strip_markdown_for_speech` (`tts.py:256`)
and `_preprocess_for_speech` (`:346`), which also shortens raw URLs and reads
domains instead of paths.

`output/tune_player.py` is separate from speech: it plays the short "thinking"
tone while a query is being processed.

## Wiring on the voice path

1. `run_reply_engine` returns reply text; the listener skips TTS entirely if the
   reply is empty or `tts.enabled` is false (the `if reply and self.tts and
   self.tts.enabled:` guard in `VoiceListener._dispatch_query`).
2. `self.track_tts_start(reply)` records the text so the echo detector knows
   what Claire is saying (`VoiceListener.track_tts_start`).
3. `self.tts.speak(reply, completion_callback, duration_callback)` is called
   from `VoiceListener._dispatch_query`. Its `_on_tts_complete` closure activates
   the hot window via `self.activate_hot_window()`.
4. `tts.interrupt()` is called when a stop command is recognised during TTS:
   both the fast text-based stop-command check and the intent judge's stop
   branch in `VoiceListener._process_transcript`.

Startup availability is decided by `daemon._initialize_tts_engine`
(`daemon.py:88`): it passes the whole `cfg` as `config=` so Fish gets its voice
id, key file, latency and model; a failure returns `None`, which
`daemon.py:1599`–`:1600` converts to `_UnavailableTTS` (`daemon.py:53`). That
object never claims to speak: `speak` logs the dropped reply
(`daemon.py:69`–`:72`), `is_speaking` is always false. A missing Fish key, an
unknown engine name or a missing local model therefore leaves Claire able to
listen and to answer in text, but silent — by design, rather than in an
unrequested voice.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `tts_engine` | `"fish"` | `fish`, `piper` or `chatterbox`. |
| `tts_enabled` | `True` | Global speech-output switch. |
| `tts_voice` | `None` | Voice name for local engines (not the Fish voice id). |
| `tts_rate` | `200` | Words per minute, used for echo-timing estimates. |
| `fish_voice_id` | `None` | The cloned Fish voice. Fish refuses to speak without it. |
| `fish_key_file` | `~/.config/claire/fish-keys.txt` | File holding the Fish API keys, one per line, comments allowed. The default is a portable `~` path, expanded at use time. Must not be group- or world-readable. |
| `fish_latency` | `"low"` | `low` or `normal`; anything else falls back to `low`. |
| `fish_model` | `"s1"` | Fish model id sent in the `model` header. |
| `tts_piper_model_path`, `tts_piper_speaker`, `tts_piper_length_scale`, `tts_piper_noise_scale`, `tts_piper_noise_w`, `tts_piper_sentence_silence` | see `config.py:650`–`:655` | Piper voice and prosody. |
| `tts_chatterbox_device`, `tts_chatterbox_audio_prompt`, `tts_chatterbox_exaggeration`, `tts_chatterbox_cfg_weight` | see `config.py:638`–`:641` | Chatterbox voice and emotion. |
| `tts_max_utterance_ms` | `3000` | Shorter capture ceiling while Claire speaks, so a stop command is heard quickly. |

Fish's own HTTP budgets are constructor defaults, not config keys:
`connect_timeout=5.0`, `first_chunk_timeout=8.0`, `total_timeout=120.0`
(`fish_client.py:418`, `:434`, `:435`). The first-byte budget is deliberately
much shorter than the total, so a server that accepted the request and went
quiet fails in seconds rather than two minutes; a construction where
`first_chunk_timeout > total_timeout` raises (`fish_client.py:464`–`:468`).

## Extension points

- **A new engine**: implement the interface, add the name to
  `SUPPORTED_TTS_ENGINES` (`tts.py:1038`), wire the branch in
  `create_tts_engine` (`tts.py:1063`), pass its config from
  `daemon._initialize_tts_engine` (`daemon.py:88`), and add the keys to
  `config.py`. `tts.spec.md` must be updated in the same change — it is the
  spec for this package.
- **Barge-in for another engine**: `flush` is the contract Fish implements;
  `interrupt` is what callers use. An engine without it stops at the end of the
  current utterance rather than immediately.
- **Prosody/markdown handling** is centralised in `_preprocess_for_speech`
  (`tts.py:346`) and is the cheapest place to change what gets read aloud.

## Failure modes

- **Fish key file missing, empty, or readable by group/others**: a named
  `FishKeyFileMissingError` / `FishKeyFileEmptyError` /
  `FishKeyFilePermissionsError` (`fish_client.py:119`, `:123`, `:127`) carrying
  the path; the engine is not built and Claire is silent.
- **No first byte within 8 s**: `FishFirstChunkTimeoutError`
  (`fish_client.py:155`); **stream not finished within 120 s**:
  `FishTotalTimeoutError` (`:159`).
- **Empty success body**: treated as the silent-empty-success defect class and
  raised, not played (`fish_client.py:22`–`:26`).
- **The response is not 16-bit mono PCM** — an MP3 body, an IEEE-float WAVE
  container, or any body the sniffer cannot parse: `FishUnsupportedAudioFormatError`
  (`fish_client.py:167`) fails that sentence and nothing is written to the
  device. This is deliberate: the alternative is a full-scale burst. A buffer
  that is not a whole number of frames is likewise reported rather than
  discarded silently.
- **PortAudio missing**: the listener's module-level audio-import guard reports
  the import error and how to fix it; capture, not TTS, is what fails.
- **An engine that cannot initialise** never starts a worker and never queues
  speech (`tts.spec.md:23`, realised by `_UnavailableTTS` at `daemon.py:53`).
- **Interrupt during synthesis**: `flush` invalidates the generation, so audio
  synthesised for the abandoned utterance is dropped rather than played late.

## Tests

`tests/test_fish_tts.py`, `tests/test_fish_client.py`, `tests/test_piper_tts.py`,
`tests/test_daemon_tts_wiring.py`, `tests/test_daemon_tts_engine_guard.py`,
`tests/test_echo_loop_prevention.py`, `tests/test_stop_command_echo.py`.

## Documentation drift

- **There is no spec file for `fish_tts.py` or `fish_client.py`.** They are over
  2000 lines together and are the default path; `tts.spec.md` states the engine
  landscape and the privacy boundary, but the two Fish modules have no spec of
  their own.
- **The factory's parameter default disagrees with the product default.**
  `create_tts_engine(engine: str = "piper", ...)` (`src/claire/output/tts.py:1064`)
  and its docstring (`:1090`) both say `piper` is the default, while the shipped
  configuration default is `"fish"` (`src/claire/config.py:635`). Behaviour is
  correct because `daemon._initialize_tts_engine` always passes `cfg.tts_engine`;
  only a bare `create_tts_engine()` call would build Piper. The docstring matches
  the signature, so this is a signature/product-default mismatch, not a stale
  docstring.
- **`tts.spec.md` claims both backends load their model "before accepting
  speech work"** (`tts.spec.md:25`). That is true of Piper and Chatterbox; the
  Fish engine reads its key file and builds its client in `start()`
  (`src/claire/output/fish_tts.py:714`–`:729`) and can fail a turn after startup
  when key rotation is exhausted.
- **The exact-duration path is Piper-only.** The listener passes
  `duration_callback` to every engine (`VoiceListener._dispatch_query`) while
  the comment beside that closure notes it is Piper-only. Fish and Chatterbox
  ignore it; the echo detector falls back to `_estimate_tts_duration`
  (`tts.py:203`).
- **`fish_key_file` defaults to a portable path, not a machine-specific one**
  (`src/claire/config.py:645`): the default is the unexpanded
  `~/.config/claire/fish-keys.txt`, which `load_settings()` expands at use time
  (`config.py:1027`, `_default_fish_key_file` at `:1345`), and
  `test_examples_config_current.py::test_fish_key_file_default_is_portable` pins
  it as portable. A fresh checkout that does not set the key file is still
  silent, with only the printed reason to explain it. The path is a location,
  not a secret; the key values inside it must never be printed.

## Related spokes

- [Listening and speech capture](listening.md) — where `speak` is called and how
  echo gating uses what was said.
- [Configuration](configuration.md) — how the keys above are loaded and
  generated.
