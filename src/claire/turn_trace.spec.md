## Per-turn latency trace spec

One structured record per answered turn, so "why was that turn slow?" is a
query rather than a bespoke probe script. Implemented in
`src/claire/turn_trace.py`; read from a shell with `scripts/turn_trace.py` and
from a UI over the control socket's `__TURN_TRACE__:` line.

### The record

One `TurnTrace` per turn, created by `run_reply_engine` when the turn begins
(step 1b, immediately after redaction). It carries:

| Field | Meaning |
|-------|---------|
| `turn_id` | Short random id for the turn |
| `logged_at` | Wall-clock UTC ISO-8601, for correlating with the journal |
| `started_monotonic` | The `time.monotonic()` zero point the offsets are from |
| `stages` | Stage name -> seconds since `started_monotonic` |
| `durations` | `router_s`, `planner_s`, `llm_s`, `speech_s`, or `null` |
| `total_s` | First stamp to last stamp, measured rather than summed |
| `route_requested` / `route_served` | The model asked for, and the model the response named as having answered |
| `cache_hit` | `true` / `false` / `null`; `null` is "the gateway did not say" (§7.4) |
| `reasoning_present`, `reasoning_chars` | Whether the answer carried reasoning, and its length |
| `question_chars`, `answer_chars` | Lengths only |
| `debug_text` | Whether the record carries question/answer text |
| `question`, `answer` | Present only when `voice_debug` is on |

Stages are stamped with `time.monotonic()`, never wall-clock, so a span
survives an NTP step. Offsets are relative to `started_monotonic`; adding the
two gives the absolute monotonic stamp back.

### Stages and where each is stamped

Canonical order (`CANONICAL_STAGES`):

1. `transcription_end` — `listener._handle_transcription_result`, via
   `turn_trace.note_transcription_end()`. Recorded before the trace exists and
   consumed by the next `begin_turn` on the same turn; it becomes the record's
   zero point, so a voice record's `transcription_end` offset is `0.0`.
2. `router_start` / `router_end` — around `tools/selection.select_tools`, by
   the `traced_stage` decorator, so every return path (and an exception) ends
   the stage.
3. `planner_start` / `planner_end` — around `reply/planner.plan_query`, the
   same way. A turn where the engine skips the planner (short tool-free query)
   has no planner stages at all, which is what happened.
4. `llm_first_token` / `llm_last_token` — in `reply/engine.chat_with_messages`,
   at the point the backend response arrives. The answer call is **not
   streamed**, so the earliest observable moment for generated text is the
   response arriving: the two stamps therefore coincide within microseconds on
   a single-call turn. `llm_first_token` keeps the first answer call's arrival
   and `llm_last_token` the most recent one, which is what a tool loop needs.
   This is a limit of the current gateway call, stated rather than papered
   over; switching the answer call to streaming is what would separate them.
5. `speech_start` — the listener's `self.tts.speak(reply, ...)` call, and the
   engine's own `tts.speak(...)` when the engine does the speaking. This is
   when speech is requested.
6. `first_audio_byte` — inside the TTS playback path, on the worker thread:
   `output/fish_tts._play_loop` on the first successful device write of a new
   utterance, `output/tts.PiperTTS._speak_once` immediately after
   `OutputStream.start()`, and `output/tts.ChatterboxTTS._speak_once`
   immediately after `pygame.mixer.music.play()`.

`speech_start` precedes `first_audio_byte` on the real path: the call is made
before the worker has a byte to write. The handover lists the two audio stages
the other way round; the stamps follow what actually happens, and the stage
*names* are the contract's.

### Stamping rules

- `mark(name)` records the **first** occurrence of a stage and ignores repeats.
  A stage that runs twice in a turn (the tool router can be re-run by
  `toolSearchTool`) keeps its first span, so the record stays stable.
- `mark_latest(name)` overwrites, for stages whose last value is the
  meaningful one (`llm_last_token` across a tool loop).
- A name outside `CANONICAL_STAGES` is refused and logged under `trace`;
  a typo cannot silently become a stage nobody reads.
- `mark_reply_audio_start()` stamps `first_audio_byte` and writes the record,
  and does nothing until `speech_start` is stamped for the turn: an
  acknowledgement clip still draining from before the reply must not be
  mistaken for the reply's audio.

### Writing

- One JSON object per turn, one line, to `~/.local/share/claire/turns.jsonl`.
  The path is overridden with `CLAIRE_TURN_TRACE_PATH`; `CLAIRE_TURN_TRACE_DISABLED=1`
  switches tracing off.
- The write happens on a single background writer thread through a bounded
  queue (`_WRITE_QUEUE_MAX`). `finish_turn` uses `put_nowait` and never waits:
  a full queue drops the record and counts the drop rather than slowing a turn.
- The file is bounded. Past `MAX_TRACE_FILE_BYTES` it rotates to a single
  `turns.jsonl.1` backup, so the pair does not grow with the number of turns.
- `run_reply_engine` schedules the write for its end of the turn
  (`finish_turn_later`, default 5 s) because the voice path speaks after the
  engine returns. The listener extends that window when it reaches the
  speaker, and the TTS first byte closes the turn immediately; a text turn
  with no speaker is written a few seconds later. The scheduler is one daemon
  thread with a small heap, not a timer thread per turn.

### Privacy

The record carries lengths and flags only. Question and answer text is added
**only** when the existing `voice_debug` setting is on (`debug.debug_enabled`),
reusing that flag rather than adding a second switch. `cache_hit` is `null`
until something actually observes a cache hit; it is never guessed.

### The summary line

`finish_turn` mirrors one line through `debug_log` in the `trace` category:
total, router, planner, llm, speech spans, the route, the cache marker, and
reasoning/question/answer character counts. It carries no text. `debug_log`
prints only with `voice_debug` on, so the line is a debugging surface; the
JSONL file is the durable one.

### Reading

- `read_records(limit, path)` returns the most recent records, oldest first,
  reading the current file and its backup; a malformed line is skipped.
- `order_problems(record)` lists missing or out-of-order stages; an empty
  record fails rather than passing by vacuity. `expected_stages_present`
  reports the stages a complete turn lacks (`voice=False` for a text turn).
- `scripts/turn_trace.py` renders them: `--last N`, `--slowest [N]`,
  `--stage router|planner|llm|speech|transcription`, `--json`, and `--check`,
  which exits non-zero when a stage is missing or out of order.
- The control socket answers `__TURN_TRACE__:{"last": N, "stage": "router"}`
  with the same records as a `__TURN_TRACE__:` IPC event, so the panel can
  show them without reading the file.
