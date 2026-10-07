# STT spec — two recognisers, one truth (D3/D4)

## Why two recognisers exist

Claire listens continuously but acts rarely. One model cannot do both jobs
well:

| | streaming (`sherpa_streaming`) | authoritative (`parakeet`) |
|---|---|---|
| engine | sherpa-onnx zipformer transducer | Parakeet v2 |
| runs | always-on, frame by frame | once per turn, on the clipped utterance |
| output status | **provisional** | **authoritative** |
| used for | wake-word matching, pre-wake context | intent, tool calls, reply, diary, graph |
| may it write memory? | **never** | yes |

The reference project deleted its second-pass recogniser over precisely the
defect this split invites: its 120-second context buffer was built from
streaming text while the turn itself carried corrected text, so the same
utterance had two versions. `TurnTranscript`
(`src/claire/listening/transcript.py`) is the defence — it carries both
texts behind named accessors with different audiences, and anything feeding
memory, the reply, or a tool call must go through
`TurnTranscript.authoritative_for`, which refuses provisional input.

Swapping a model — including dropping in a finetuned Parakeet — is a
configuration change (`stt_model_path`, `streaming_stt_model_dir`), never a
code change. `identity()` on either engine reports what actually answered.

## Engine contract

Both engines are reached through the protocols in
`src/claire/listening/stt/base.py`; factories live in
`src/claire/listening/stt/__init__.py` and must never fail at import time
(heavy imports stay inside the engines).

### Streaming: `SherpaStreamingEngine` (`stt/sherpa_streaming.py`)

```python
SherpaStreamingEngine(model_dir: str, sample_rate: int = 16000, num_threads: int = 2)
engine.name  # 'sherpa_streaming'
engine.accept_waveform(samples: np.ndarray, sample_rate: int) -> None
engine.partial_text() -> str   # best transcript so far, wake matching only
engine.finalize() -> str       # flush endpoint, return segment text, reset
engine.reset() -> None         # drop stream state, start a new segment
engine.identity() -> SttIdentity
```

- `model_dir` holds `tokens.txt` plus encoder/decoder/joiner `.onnx` files.
  The loader prefers `*.int8.onnx` variants (faster on CPU) and falls back to
  plain `*.onnx`.
- Built via `sherpa_onnx.OnlineRecognizer.from_transducer(...)` with endpoint
  detection enabled: rule1 min trailing silence 1.2 s, rule2 0.6 s, min
  utterance length 2.0 s — an utterance is declared finished on silence so the
  wake loop can `finalize()` the segment on pauses.
- `accept_waveform` takes mono float32 in `[-1, 1]`; off-rate input is
  resampled (linear interpolation, numpy only); stereo raises
  `SttDecodeError`. Each frame is decoded immediately so `partial_text()` is
  fresh for wake matching.
- `finalize()` decodes, reads the result, then `reset()`s: every segment
  leaves the engine clean for the next one.
- **Empty-stream guard:** sherpa-onnx aborts the process (`LOG(FATAL)` in
  `features.cc`) when `decode_stream`/`get_result` runs on a stream that never
  received a frame. `partial_text()`/`finalize()` on an unfed segment therefore
  short-circuit to `""` without touching the recognizer. This is the one place
  an empty string is legitimate: it means "no audio fed", not "model missing".
- Wiring: `create_streaming_stt_engine(cfg)` reads `streaming_stt_engine`
  (`"sherpa"` or `""` = disabled → returns `None`, never a stub),
  `streaming_stt_model_dir`, `sample_rate`, `streaming_stt_num_threads`.

### Authoritative: Parakeet (`create_stt_engine`)

Utterance-in, `SttResult`-out. The text it produces is the one used for
intent, tool calls, the reply, and memory. (Implemented separately; this spec
fixes the split, not the model.)

### Opt-in only: Whisper (`stt/whisper_engine.py`)

Whisper is **not** a fallback and not a second authoritative path. It exists for
anyone who deliberately sets `stt_engine='faster_whisper'` (or `'whisper'`), and
`create_stt_engine` returns it only for those spellings — any other token raises
`SttError` rather than quietly selecting an engine.

A Parakeet-configured process must not pay for it: nothing in
`claire.listening.listener` or `claire.daemon` imports `faster_whisper` or
`mlx_whisper` at module load, the startup Whisper load runs only when
`stt_engine` names Whisper (`VoiceListener._configures_whisper`), and the
listener's decode path never calls the Whisper decoder when Parakeet is
configured. When Parakeet fails, the turn is empty and the failure is reported
through `_note_stt_degradation` — an unavailable recogniser is visible, never
papered over by a worse one.

## Provisional / authoritative split

- `partial_text()` and `finalize()` return **provisional** text. It feeds wake
  detection and pre-wake context, and must never be written to memory, the
  diary, the knowledge graph, or a tool call.
- `TurnTranscript.provisional_text` vs `TurnTranscript.authoritative_text`
  carry the two versions; `authoritative_for` refuses provisional input.
- The divergence suite (`tests/test_transcript_divergence.py`) feeds
  deliberately contradictory transcripts and asserts every memory-facing
  surface used the authoritative one.

## Named-failure rule

Every engine failure is a *named* error from `base.py`:

- `SttModelMissingError(path, hint)` — `model_dir` missing, or `tokens.txt` /
  encoder / decoder / joiner absent. The message names the exact path looked
  for and how to obtain the model.
- `SttBackendUnavailableError('sherpa-onnx', 'sherpa-onnx')` — the package is
  not importable. Importing `claire.listening.stt` itself never fails; the
  error surfaces at engine construction.
- `SttDecodeError` — the model was present but audio could not be processed
  (e.g. stereo input).

A missing model, binary, or credential **must raise**; it must never return an
empty-but-successful transcript. An empty transcript means "the user said
nothing", so silent empty success would be indistinguishable from silence —
the known upstream defect this rule exists to prevent.

## Verification

- Unit layer (`tests/test_sherpa_streaming.py`, `pytest.mark.unit`): fake
  `sherpa_onnx` backend via monkeypatch — named failures, int8/plain file
  selection, protocol conformance, partial/finalize/reset semantics, stereo
  rejection, off-rate resampling. No model, no network.
- Integration layer (`pytest.mark.integration`): the genuine streaming
  zipformer bundle (read-only, outside the repo) plus real speech from its
  `test_wavs/`, read with stdlib `wave` (16-bit PCM → float32) and fed frame
  by frame. Observed 2026-10-06: `0.wav` → `THE YELLOW LAMPS WOULD LIGHT UP
  HERE AND THERE THE SQUA…`; `1.wav` → `…LOVELY CHILD…`; `8k.wav` (8 kHz,
  upsampled) → weak `S AFFECTED`. Tests assert non-emptiness plus these
  content words.
- Fail-proof: the suite was run red against a deliberately wrong
  implementation (missing-model error swallowed, `finalize()` returning `''`)
  — 6 failed — then green (19 passed) after restore.
