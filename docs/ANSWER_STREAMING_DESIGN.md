# Design: speaking the answer while it is still being written

Status: **refuted by measurement, not implemented.** The design below is kept because it
names the risks accurately, but the measurement in the next section removes its premise: the
answer call is mostly fixed gateway overhead, so streaming it would save far less than it
costs. Do not implement this without new evidence that the balance has changed.

## The measurement that retired it

Run on this machine against the live gateway and the **real captured answer prompt** (3,257
prompt tokens, 3,200 of them served from the gateway's cache), streaming enabled so every
phase could be timed:

| prompt | prompt tokens | cached | first token | first content | total |
|---|---|---|---|---|---|
| real captured answer prompt | 3,257 | 3,200 | 1.25 s | 1.32 s | 1.79 s |
| the same prompt again | 3,257 | 3,200 | 1.20 s | 1.49 s | 1.92 s |
| the real turn with the system prompt cut to 600 chars | 326 | 0 | 1.10 s | 1.38 s | 2.81 s |
| a deliberately tiny prompt | 48 | 0 | 0.92 s | 2.24 s | 2.62 s |

Three conclusions, none of which the original estimate had:

1. **There is about a second of fixed overhead before any token comes back**, and it does not
   depend on prompt size: 48 tokens cost 0.92 s, 3,257 tokens cost 1.25 s. That is the
   gateway and the model being scheduled, not the prompt being read.
2. **Prompt size is therefore not worth optimising either.** Shrinking a 3,257-token prompt to
   326 tokens moved time-to-first-token by 0.15 s. The gateway already caches the large
   system prompt (3,200 of 3,257 tokens were cached).
3. **Time-to-first-content is dominated by variable reasoning, and the prose itself is
   short.** Only about half a second of a 1.8 to 2.8 s call is spent producing the words.
   Reasoning-token counts across runs of the same prompt ranged from 8 to 195, and the
   gateway ignores every instruction to reason less (`reasoning_effort`, `enable_thinking`,
   "answer immediately"), which earlier rounds established.

So the speakable window is roughly the last half second of the call. Streaming it would move
the first sentence earlier by a few hundred milliseconds at best, while the change requires a
new streaming entry point that takes a message list, an incremental sentence splitter, and
echo bookkeeping that one `speak()` per sentence would otherwise break, including the stop
command. That is a bad trade, and it is the reason nothing here is implemented.

The honest description of where a conversational turn's time now goes: the collection wait
(the owner's setting), transcription, about a second of fixed gateway overhead per call,
variable reasoning, and a short generation. Streaming attacks only the last of those.

## The original argument, for the record

The case for it was this. On a conversational turn the router and the planner are now both
skipped, so the whole turn is a single model call that takes about 3 s and writes two or three
sentences. The speech engine already synthesises and plays per sentence, so it could start on
sentence one while sentence two is still being generated. The estimate at the time was a
saving of roughly one second on every spoken turn. It was wrong, for the reason above.

### Why it would have been a large change

1. **The live speaker is not the reply engine.** `run_reply_engine` is called with
   `tts=None` on both live paths (voice: `listening/listener.py` `_dispatch_query`; text:
   `daemon.py`). `reply/engine.py`'s own `tts.speak` is dead code on those paths. The
   listener speaks (`self.track_tts_start(reply)` then
   `self.tts.speak(reply, completion_callback=..., duration_callback=...)`). Any design that
   stays inside the engine changes nothing audible.
2. **The answer is produced by the non-streaming tool-calling call.** The final spoken
   string is the loop's `chat_with_messages` content turn. There is no separate final
   synthesis pass.
3. **The existing streaming call does not fit.** `LLMBackend.streaming` takes a system
   prompt and one user string, with no `messages` and no `tools`, and discards everything
   except `delta.content`. Its only consumer is `memory/conversation.py`.
4. **The sentence splitter is whole-text only.** `output/fish_tts.py::_split_sentences`
   handles abbreviations, decimals, short-fragment merging and hard splits, and is the right
   helper to reuse, but it must be driven incrementally: its boundary regex ends `(\s+|$)`,
   so a `$` match means end-of-buffer, not end-of-sentence. It must also run on text that has
   been through `_preprocess_for_speech`, or markdown spanning a boundary is mangled.
5. **Echo detection keys off the spoken text and a start timestamp.** The listener compares
   heard speech against `EchoDetector._last_tts_text`, and treats `_tts_start_time` as a
   session identity (`same_tts_context`). `track_tts_start` resets both. One `speak()` per
   sentence with one `track_tts_start` per sentence would therefore:
   - leave `_last_tts_text` holding only the last sentence, so a late echo of the first
     sentence is not recognised;
   - break `same_tts_context` for a transcript captured during sentence one but processed
     after sentence two, which blanks the judge's echo context, disables the post-TTS tail
     guard, and stops `active_tts_overlapped` being true. That last one gates the stop
     command path, so **saying "stop" over a multi-sentence reply would stop interrupting
     her**.
   The acoustic gate (`captured_during_tts`, computed at capture time) survives, so an echo
   captured while she is still speaking is still rejected.
6. **Callbacks and speaking state are per `speak()` call.** `completion_callback` fires once
   per queued item, and `is_speaking()` clears when the pending count hits zero. One call
   per sentence would open the hot window mid-reply, stamp `_last_tts_finish_time` early,
   and let `is_speaking()` flicker between sentences. That flicker feeds the acknowledgement
   suppression, the barge-in capture window, the phone echo gate and the face widget.
   `duration_callback` sets the *total* duration used for words-per-second maths, so it must
   not be fed one sentence's length.

## The design, had it been worth building

Stream only where it is safe, and keep every existing consumer of the whole reply intact.

1. **Gate the feature.** Stream only when the router returned no real tools and the query is
   short, i.e. the existing `_skip_planner` condition, and only when text-based tool calling
   is not in use. On that path the first model call is already the answer. Accumulate the
   deltas and run the existing `_extract_structured_tool_call` / `_extract_text_tool_call`
   checks before speaking anything, so a surprise tool call falls back to `tts.flush()` and
   the ordinary non-streamed handling rather than speaking a half-answer.
2. **Add one streaming entry point that takes messages.** Either extend `streaming` or add a
   sibling that accepts the message list, so the loop's real call can stream. The gateway's
   streamed tool-call delta shape is unverified on this host, which is the main reason the
   gate above is narrow.
3. **Reuse the splitter, incrementally, after preprocessing.** Extract an incremental
   variant of `fish_tts._split_sentences` (or feed the accumulator to it and cut only
   complete sentences, treating a `$` match as provisional).
4. **Keep the echo detector whole.** Add `EchoDetector.extend_tts_text(delta)` that appends
   to `_last_tts_text` and touches nothing else. Call `track_tts_start(first_sentence)`
   once, then `extend_tts_text` before each later `speak()`. Leave `_tts_exact_duration`
   unset for streamed replies. Attach `completion_callback` to the final sentence only.
5. **Prefer an incremental API inside the speech engine.** `begin_utterance()` / `feed()` /
   `end_utterance()` so that one queued item spans the whole reply: the pending count stays
   at one, `is_speaking()` cannot flicker, completion fires once naturally, and the stored
   spoken text is the accumulated reply. This removes three classes of caller-side bug at
   source and is the recommended shape.
6. **Plumbing.** Add an optional `on_sentence` callback to `run_reply_engine`; the listener
   passes a closure doing track / extend / speak. Keep the full reply as the return value,
   keep the single `speak()` as the fallback when there is no sink, when streaming is off, or
   when the stream fails part-way (a partial failure must be reported, never spoken as
   complete). Respect `llm_chat_timeout_sec`. The panel and desktop stay on the single
   completion event.

## Tests it would have needed

New, in `tests/test_echo_detection.py`: a multi-sentence `track_tts_start` plus
`extend_tts_text` case asserting `_last_tts_text` equals the whole reply and that extend does
not move `_tts_start_time`. In `tests/test_echo_loop_prevention.py`: an echo of the first
sentence arriving after all speech has finished must still be rejected, and "stop" spoken
over sentence two of a three-sentence reply must still interrupt. Streaming itself needs a
backend test plus a sink test in `tests/test_voice_dispatch.py`.

Note the coverage gap this closes: every existing echo test models a single-sentence reply,
so none of them would catch the regression in (5).

Must keep passing: `tests/test_fish_tts.py` (splitter order, abbreviations, hard splits,
pipelining, flush, interrupt, `get_last_spoken_text`), `tests/test_piper_tts.py`,
`tests/test_tts_factory_fish.py`, `tests/test_tts_preprocessing.py`,
`tests/test_voice_dispatch.py`, `tests/test_nonblocking_voice_capture.py`,
`tests/test_echo_detection.py`, `tests/test_echo_loop_prevention.py`,
`tests/test_short_query_echo.py`, `tests/test_stop_command_echo.py`.

## Open questions it left

- The streamed tool-call delta shape from this gateway is unverified, so the flush-on-
  surprise fallback is a precaution, not a proven path.
- The p50 figures behind the expected saving are from live measurement, not a stored
  benchmark; there is no perf fixture in the repository.
- Whether the speech engine's backpressure can block the generation thread if sentences are
  enqueued faster than they play was not fully traced. It matters, because `speak()` would
  be called from the thread running the model stream.
