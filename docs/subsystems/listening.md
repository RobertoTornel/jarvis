# Listening and speech capture

**Authoritative spec:** [`src/claire/listening/listening.spec.md`](../../src/claire/listening/listening.spec.md),
[`turn.spec.md`](../../src/claire/listening/turn.spec.md),
[`stt/stt.spec.md`](../../src/claire/listening/stt/stt.spec.md).
Read this when you are changing how audio is captured, where an utterance ends,
what counts as speech, or how a transcript becomes a query.

Specs next to the code win over this document. Where this page and a spec
disagree, the spec and the code decide; discrepancies found while writing this
page are listed at the end.

## Responsibility

The listening subsystem owns everything between the microphone and a dispatched
query: device selection, the PortAudio capture stream, frame framing, voice
activity detection, utterance boundary assembly, the two speech gates that
decide whether a buffer is worth transcribing, the two recognisers, the wake
word and stop-command text matchers, the echo detector that stops Claire waking
herself, the transcript buffer that gives the intent judge its context, and the
intent judge that decides whether the last utterance was addressed to her.

It does *not* own the reply: once a query is accepted, `_dispatch_query` calls
`run_reply_engine` and the TTS engine directly (both in
`src/claire/listening/listener.py`). The listener is the
component that wires listening to replying and speaking.

## The pipeline, in capture order

| Stage | Where | What happens |
|---|---|---|
| Device selection | `listener.py` `_run` -> `utils/audio_capture.py:9` `resolve_input_device`, `:80` `open_input_stream` | The configured or system-default input is resolved to a concrete index before the permission probe and model loading; name matching skips output-only devices. Multichannel capture is averaged with `mono_capture` (`utils/audio_capture.py:120`). |
| Capture callback | `listener.py` `_on_audio` | PortAudio hands over `(indata, frames, time_info, status)`; the callback stays cheap and enqueues raw blocks. |
| Framing and VAD | `listener.py` `_consume_audio_frames`, `_is_speech_frame` | Blocks are cut into configured 10/20/30 ms frames; `webrtcvad` (aggressiveness from config) classifies each frame, with a resample to 16 kHz for the VAD. |
| Pre-roll | `listener.py` `_consume_audio_frames` (`vad_pre_roll_ms`) | `vad_pre_roll_ms` (default 240) of audio before the first speech frame is retained, so the utterance does not start mid-word. |
| Endpointing | `listener.py` `_consume_audio_frames` (`endpoint_silence_ms`, `max_utterance_ms`, `tts_max_utterance_ms`) | Speech ends after `endpoint_silence_ms` (default 800) of non-speech, or at `max_utterance_ms` (default 12000), shortened to `tts_max_utterance_ms` (default 3000) while Claire is speaking. |
| Speech gate | `listener.py` `_speech_gate_reason`, `_discard_noisy_utterance` | Three language-agnostic tests before any recogniser runs: at least `min_speech_ms` (default 250 ms) of VAD-positive audio, and speech RMS that clears **both** an absolute floor (`noise_gate_min_rms`, default 0.002) and the room's own noise floor times `noise_gate_margin`. A buffer that fails is dropped without a transcription and without a "Heard:" line. |
| Utterance finalisation | `listener.py` `_finalize_utterance` | The buffered audio and its timing metadata become a transcription job on `_transcription_jobs_q`, tagged with the capture generation. |
| Authoritative STT | `listener.py` `_run_transcription_worker`, `_decode_utterance`, `_get_authoritative_stt_engine` | A worker thread decodes the turn with the authoritative recogniser (Parakeet by default). A job whose capture generation is stale is discarded at both ends of the decode. |
| Turn construction | `turn.py` (`TurnAssembler`), `listener.py` `_build_turn` | Provisional streaming text and authoritative text are bound into one `TurnTranscript`; the guarded accessors are the only way to reach the text the turn acts on. |
| Transcript buffer | `transcript_buffer.py` | Completed segments (default `transcript_buffer_duration_sec`: 120 s) are appended for the intent judge's context. |
| Gates and wake detection | `listener.py` `_process_transcript`, the acoustic echo gate, `_wake_word_seen` | Stop command, acoustic echo gate, text echo checks, intent judge, wake word, collection mode — in that priority order. |
| Dispatch | `listener.py` `_dispatch_query` | The accepted query is handed to `run_reply_engine` and the reply to the TTS engine (`tts.speak(reply, ...)`). |

### Why the gate has both an absolute and a relative level test

A quiet room and a loud one fail the same gate in opposite directions, so one
level test cannot cover both.

* **Relative** (`noise_gate_margin`): in a loud room the room's own noise floor
  is high, and speech has to stand above *it*. This is the test that keeps a
  television or a conversation in the background from being transcribed.
* **Absolute** (`noise_gate_min_rms`): in a quiet room the 20th-percentile floor
  collapses to near zero, so `floor * margin` becomes smaller than the
  converter's own dither and the relative test passes everything. Measured on a
  real microphone at 47% input volume: room floor 0.00008, dither 0.0002-0.0015,
  and a recogniser fed that dither answered with its priors - `"Yeah."` eight
  times, `"Okay."` four times, plus `"Uh."`, `"Oh."` and `"Mm-hmm."`, 22 such
  utterances in seven minutes, none of them speech. The same microphone measured
  real speech at 0.00226-0.00926, so the shipped 0.002 floor sits above every
  observed hallucination and below the quietest real utterance. A microphone
  quieter than that floor is what `noise_gate_min_rms: 0.0` is for.

An *accepted* utterance logs the same numbers as a rejected one
(`utterance accepted: buffered ... ms, vad speech ... ms (audible floor ...,
room floor ... x..., speech rms ...)`), which is what makes a "she is hearing
things" report a measurement rather than a guess.

## The two recognisers

`turn.spec.md` defines the split; the code enforces it.

| Engine | `stt_engine`/`streaming_stt_engine` | Role | Status of its text |
|---|---|---|---|
| sherpa-onnx zipformer transducer | `streaming_stt_engine` (default `"sherpa"`) | Always-on framing, wake detection, pre-wake context | **provisional** — a guess |
| Parakeet v2 | `stt_engine` (default `"parakeet"`) | Intent, tools, reply, memory | **authoritative** — the text the turn acts on |

Provisional text is never persisted and never acted on. The turn's artefacts —
the intent-judge input, tool arguments, spoken reply, diary row and graph entry —
are derived from `TurnTranscript.authoritative_text` through guarded accessors;
passing provisional text into any of them raises `ProvisionalTextRejected`
(`src/claire/listening/transcript.py:85` `authoritative_for`, raising `ProvisionalTextRejected` at `:41`).

Whisper (`stt_engine="faster_whisper"`) still exists but is **opt-in only**: it
is built when, and only when, the config names it. It is never a fallback. A
Parakeet failure is reported as a named degradation
(`listener.py` `_get_authoritative_stt_engine`, which sets
`_authoritative_stt_failed` and a reason) rather than being
answered by a second engine.

Engine construction lives in `src/claire/listening/stt/__init__.py`
(`create_stt_engine`, `create_streaming_stt_engine`); the implementations are
`stt/parakeet.py`, `stt/sherpa_streaming.py` and `stt/whisper_engine.py`, all
behind `stt/base.py` (`SttEngine`, `StreamingSttEngine`, `SttResult`).

## The speech gates

Order matters, and the echo decision is deliberately **acoustic**, not textual.
`_process_transcript` runs these branches in this order:

1. **Early echo check and wake-word beep** (in `listener.py`). For an utterance
   that did not overlap TTS, a fuzzy text-echo match against the last TTS text
   is dropped before anything else, after trying `cleanup_leading_echo` and
   `salvage_after_echo_tail`; otherwise an explicit wake word starts the early
   beep. This keeps a false beep — and the judge's latency — off an echo.
2. **Stop command.** `wake_detection.is_stop_command` (`wake_detection.py:201`)
   and `is_stop_command_echo` (`:240`) run while Claire is speaking.
   A recognised stop calls `tts.interrupt()`, which Fish TTS
   implements as an immediate `flush` (`fish_tts.py:827`).
3. **Text echo rejection during TTS** (`echo_detection.py:505`
   `should_reject_as_echo`, then `:216` `cleanup_leading_echo_during_tts`). A
   transcript that is a literal echo is dropped; user speech appended after an
   echo tail is salvaged when it clears `EchoDetector.min_salvage_words`.
4. **Acoustic echo gate** (in `listener.py`), which runs before the text salvage
   below and before the intent judge on purpose. The decision is made on *when*
   the audio was captured relative to Claire's own speech: `captured_during_tts`
   is set at capture time from the TTS clock, and an utterance that begins after
   she finishes is still rejected until the post-TTS tail guard has passed. Text
   similarity cannot recognise a garbled echo, so no text-only check is trusted
   here.
5. **Text salvage from merged echo and speech** (`echo_detection.py:415`
   `_salvage_suffix_from_echo`, with `:324` `salvage_after_echo_tail` as the
   rightmost-boundary fallback), which strips a leading echo from a transcript
   that also contains user speech.
6. **Intent judge** (`intent_judge.py:151` `IntentJudge`, `:397` `judge`,
   created by `create_intent_judge` at `:550`, instantiated in `listener.py`
   as `self._intent_judge = create_intent_judge(self.cfg)`). It receives the
   transcript buffer, the last TTS text and the hot-window state, and returns an
   `IntentJudgment` (`intent_judge.py:122`)
   plus a failure reason (`:241`). It is skipped for utterances of three words
   or fewer during TTS (in `listener.py`'s `_process_transcript`), which are
   handled as stop commands. After a directed verdict, `echo_detection.query_is_prior_echo`
   (`echo_detection.py:100`) re-checks the extracted query against the last TTS text.
7. **Wake word** (`listener.py` `_wake_word_seen` ->
   `wake_detection.is_wake_word_detected`, `wake_detection.py:140`). Detection is
   text-based over the live transcript, whole-word, with phonetic folding and a
   fuzzy ratio floor. `extract_query_after_wake` (`:176`) strips the wake word
   and hands the remainder to collection mode.
8. **Hot window / collection** (`state_manager.py:19` `StateManager`, `:12`
   `ListeningState`). After Claire finishes speaking, a wake-word-free follow-up
   is allowed for `hot_window_seconds` after the echo settles; up to
   `hot_window_max_follow_ups` consecutive follow-ups (default `3`, `0` means
   unlimited) are admitted per explicit wake word. Past the cap the chain is
   refused (`Follow-up window closed`), so the wake word must be said again.
   The module constant `MAX_CONSECUTIVE_HOT_WINDOW_TURNS = 1` (`listener.py`)
   survives only as the fallback for a config that predates the key.
9. **Ignore.** Anything else is logged as `input ignored (no wake word ...)`.

## Configuration keys that affect it

All names are from `src/claire/config.py`'s default mapping (the `_default_config`
literal) and its coercion block.

| Key | Default | Effect |
|---|---|---|
| `sample_rate` | `16000` | Requested capture rate. |
| `voice_min_energy` | `0.02` | Energy floor used by capture-side speech accounting. |
| `vad_enabled` | `True` | Turns WebRTC VAD on or off. |
| `vad_aggressiveness` | `2` | `webrtcvad.Vad` mode (0–3). |
| `vad_pre_roll_ms` | `240` | Audio kept before the first speech frame. |
| `min_speech_ms` | `250.0` | Minimum VAD-positive audio for an utterance to be transcribed. |
| `noise_gate_enabled` / `noise_gate_margin` | `True` / `2.0` | Room-noise-floor gate. |
| `noise_gate_min_rms` | `0.002` | Absolute level floor for the gate: below this the input is the converter, not a voice. `0.0` disables the test; a value that cannot be a voice level (non-finite, or at/above half of full scale) is ignored in favour of the default. |
| `endpoint_silence_ms` | `800` | Silence that ends an utterance. |
| `max_utterance_ms` | `12000` | Hard utterance ceiling. |
| `tts_max_utterance_ms` | `3000` | Shorter ceiling while Claire is speaking. |
| `wake_word` / `wake_aliases` | `"claire"` / seven mishearings | Text wake matching. |
| `wake_fuzzy_ratio` | `0.82` | Similarity floor for approximate wake matches. |
| `stop_commands` | list | Stop phrases matched during TTS. |
| `hot_window_enabled` | `True` | Arms the wake-word-free follow-up window after each reply. |
| `hot_window_seconds` | `3.0` | Follow-up window length, clamped on load to `[1.0, 30.0]` (the range the settings UI declares). |
| `hot_window_max_follow_ups` | `3` | Consecutive wake-word-free follow-ups per wake word; `0` means unlimited. Wired to `StateManager` as `hot_window_turn_limit`, falling back to the module constant `MAX_CONSECUTIVE_HOT_WINDOW_TURNS = 1` (`listener.py`) if the key is absent. |
| `hot_window_require_directed` | `False` | Opt-in strictness: when `True`, an utterance in the window must be judged `directed` at Claire (or carry a genuine wake word) to be accepted, so background television cannot drive a turn. `False` keeps the permissive behaviour: all non-echo speech counts as a follow-up. |
| `transcript_buffer_duration_sec` | `120.0` | Intent-judge context window. |
| `stt_engine` / `stt_model_path` / `stt_model_kind` | `"parakeet"` / path / `"auto"` | Authoritative recogniser. |
| `streaming_stt_engine` | `"sherpa"` | Always-on provisional recogniser; `""` disables it. |
| `intent_judge_timeout_sec` | `6.0` | Judge budget; a timeout is a named, non-fatal failure. |
| `echo_tolerance` | float | Tail after TTS during which audio still counts as overlapping. |
| `voice_debug` | env `CLAIRE_VOICE_DEBUG=1` | Verbose voice logging. |

## Extension points

- **A new recogniser**: implement `SttEngine`/`StreamingSttEngine` from
  `stt/base.py` and register the config token in `stt/__init__.py`'s
  `create_stt_engine`. Whisper is the worked example of an opt-in engine.
- **A new wake rule**: `wake_detection.is_wake_word_detected` and
  `extract_query_after_wake` are pure text functions with their own tests, so a
  rule change does not touch the audio path.
- **A new gate**: gates are ordered branches in `_process_transcript`
  (`listener.py`). A new gate belongs there, above the intent judge, and
  needs a reason string for the debug log.
- **A phone or panel input**: those paths enter through the same state manager
  and dispatch; see the [phone bridge](phone-bridge.md) and [panel](panel.md)
  spokes.

## Failure modes

- **Microphone missing or unopenable**: capture reports the failure through
  `_check_audio_health` (`listener.py`), which warns when no callback has
  arrived for five seconds; a missing named device is an actionable error, not a
  silent switch to another input.
- **Audio worker dies**: `_run_audio_worker` (`listener.py`) catches the
  exception, prints that listening has stopped, and calls `stop()`; it does not
  silently continue.
- **Authoritative STT unavailable**: the turn is not answered by a second
  engine. `_authoritative_stt_failed` and `_authoritative_stt_reason` record why.
- **VAD unavailable**: `webrtcvad` failing to import leaves `self._vad = None`;
  the energy path still frames audio, and the failure is reported once when the
  import fails rather than per frame.
- **Intent judge unavailable or timing out**: the wake-word path is the
  fallback, and the reason is surfaced from `IntentJudge.last_failure_reason`.
- **Everything looks like echo**: the tail guard rejects it and the debug log
  names the reason; the post-TTS tail guard exists so a genuinely late utterance
  is not lost forever.

## Tests

`tests/test_voice_listener.py`, `test_listener_two_recognisers.py`,
`test_parakeet_engine.py`, `test_check_stt_model.py`,
`test_transcript_buffer.py`, `test_transcript_divergence.py`,
`test_wake_detection.py`, `test_wake_alias_boundaries.py`,
`test_wake_false_positives.py`, `test_echo_detection.py`,
`test_echo_loop_prevention.py`, `test_short_query_echo.py`,
`test_stop_command_echo.py`, `test_intent_judge.py`,
`test_audio_capture_pipeline.py`, `test_listening_ux_overhaul.py`.

## Documentation drift

- **Barge-in latency depends on the configured engine.** Whether
  `tts.interrupt()` cuts audio off within tens of milliseconds depends on the
  engine, and only the Fish engine implements `flush`
  (`src/claire/output/fish_tts.py:827`).
- **`listener.py` fallback defaults differ from the config defaults** for the
  wake ratio, the voice-energy floor and the LLM warmup probe:
  `getattr(self.cfg, "wake_fuzzy_ratio", 0.78)` versus
  `"wake_fuzzy_ratio": 0.82` at `src/claire/config.py:677`;
  `getattr(self.cfg, "voice_min_energy", 0.0045)` versus
  `"voice_min_energy": 0.02` at `src/claire/config.py:661`; and
  `getattr(self.cfg, "llm_tools_timeout_sec", 8.0)` versus
  `"llm_tools_timeout_sec": 15.0` at `src/claire/config.py:613`. The config
  value wins in practice because `Settings` always carries the attribute; the
  stale fallbacks only show themselves in a hand-built settings object.
- **`reply.spec.md` names `src/claire/system_prompt.py` as the single unified
  `SYSTEM_PROMPT`** (`src/claire/reply/reply.spec.md:8`) while the prompt is
  actually assembled per model size from `src/claire/reply/prompts/`
  (`system.py`, `model_variants.py`). See the
  [intent and reply spoke](intent-and-reply.md).

## Related spokes

- [Intent and the reply loop](intent-and-reply.md) — what happens after dispatch.
- [Speech output](speech-output.md) — the streaming synthesis path the echo
  gates are built around.
- [Configuration](configuration.md) — where these keys come from.
