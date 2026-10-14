# Turn assembly contract (`src/claire/listening/turn.py`)

Decision D3 gives Claire **two recognisers with different jobs**:

| engine | role | output status |
|---|---|---|
| streaming (sherpa-onnx zipformer), always on | wake detection, pre-wake context | **provisional** — a guess |
| Parakeet v2, per turn | intent, tools, reply, memory | **authoritative** — the text the turn acts on |

## The defect this prevents

The reference project deleted its second-pass recogniser over exactly this
bug: its 120-second context buffer was built from **streaming** text while the
turn itself carried **corrected** text. The same utterance therefore had two
versions — Claire could book "a table for four" while remembering "a table for
two". Acting on one text and remembering another is not a cosmetic mismatch;
for tool calls it is acting on an instruction the user never gave.

## The rule

**Provisional text is never persisted, and never acted on.** It may be used
for exactly two things: detecting the wake word, and labelling pre-wake audio
as overheard context. Everything downstream of the turn —

- the intent-judge input (`intent_text`),
- the tool-call arguments (`tool_arguments`),
- the spoken reply (`reply_text`),
- the diary row (`diary_row`) and the knowledge-graph entry (`graph_entry`)

— is derived from `TurnTranscript.authoritative_text` through the transcript's
guarded accessors. Passing provisional text into any of those paths raises
`ProvisionalTextRejected` (a programming error, loud by design — see
`transcript.py:authoritative_for` and `TurnTranscript.tool_arguments`).

`TurnAssembler` is deliberately thin (~100 lines, no Qt/audio/network): it
constructs `TurnTranscript` turns and delegates every artefact to the
transcript's guards rather than reimplementing them, so there is exactly one
place where the guarantee lives.

## Degraded turns are honest

When the Parakeet engine cannot run, `build_degraded` promotes the streaming
text — but the turn carries `source="streaming_fallback"` plus the reason, and
`degraded` is `True`. A fallback turn must never be mistaken for a verified
one downstream (e.g. the reply should hedge). An empty Parakeet result is a
`ValueError`, never an empty transcript: "the user said nothing" must not be
the silent rendering of an engine failure.

## Pre-wake context is overheard, not quoted

`hearing_context` labels buffered audio as overheard hearing and states it is
not the user's own words. Downstream prompts must never present it as
something the user said — that would be a second source of false memory.

## Memory rows are auditable

Every diary row and graph entry carries `source` and `divergent`, and never
carries `provisional_text`. A stored memory can therefore be audited later:
which engine produced it, and whether the two recognisers disagreed.

## Proof

`tests/test_transcript_divergence.py` feeds deliberately contradictory inputs
(streaming *"book a table for two"* vs Parakeet *"book a table for four"*) and
asserts end to end that the tool call, reply, diary row and graph entry all
use the Parakeet text; that no memory field contains the streaming text; that
each downstream path raises `ProvisionalTextRejected` on provisional input;
that hearing context is labelled overheard; that degraded turns are marked;
and that memory rows carry `source`/`divergent`. Per the work order the suite
was first run against a deliberately naive assembler (streaming text used
everywhere): **14 failed**. Against the real assembler: **14 passed**.

Not wired into `listener.py` or `daemon.py` by design — the module stays
self-contained until its caller is ready.

## Relationship to the per-turn latency trace

This module assembles the turn; it stamps nothing. The turn's
`transcription_end` stage is stamped by the listener when it receives the final
result (`listening.spec.md`, "Per-turn latency trace"), and the reply engine
opens the rest of the record. See `src/claire/turn_trace.spec.md`.
