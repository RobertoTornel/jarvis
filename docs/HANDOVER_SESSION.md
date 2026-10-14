# Claire: session handover, 2026-10-14

**This page records one working session's progress against the next-work programme in
[`HANDOVER_GENERAL.md`](HANDOVER_GENERAL.md) §7, and it is deliberately blunt about what is
verified and what is not.** The general handover remains the description of the system; this page
is the state of the work. Where the two disagree, the general handover is right about the system
and this page is right about the work.

- **Repository:** `/home/mana/Documents/jarvis_cloud`, checkout now at `abb6da5` on `main`; this is
  not the HEAD recorded in the previous session. No changes have been committed or pushed in this
  continuation.
- **Working tree: dirty on purpose.** This continuation preserves the prior uncommitted bundle and
  adds the turn-trace test correction, the outcome eval harness, Tailscale-aware bridge handling, and
  documentation updates. Review and commit as a coherent unit only after the final gate and human review.
- **The live system was never touched.** `claire`, `claire-panel` and `claire-desktop` were not
  restarted or controlled, no window was opened, and the owner's real `~/.local/share/claire/turns.jsonl`
  has not been read or written. The full pytest gate did trigger a native audio test that attempted to
  open a sounddevice output stream and aborted; no deliberate audio-device operation was performed.

---

## 1. What was asked

Take over per `HANDOVER_GENERAL.md` §10 and work the §7 programme in the written order — §7.1 turn
tracing first, then §7.2 (eval harness), then §7.4 (semantic-cache visibility); §7.3 is a documented
negative result and is **not** to be rebuilt — plus the §8 small well-scoped fixes. Subagents do the
work; this continuation preserves the user's no-commit instruction, reviews the implementation, and runs the full gate serially.

## 2. What is finished

### 2.1 General handover status

The revised `HANDOVER_GENERAL.md` is inherited uncommitted content and has been updated during this
continuation. No commit or push was made.

### 2.2 The §8 documentation bundle (working tree, uncommitted, reviewed)

Four documents were corrected. The bundle is complete; it was reviewed past the subagent's own
report, and its diffs read correctly.

| file | change |
|---|---|
| `STATUS.md` (+ the copy at `/home/mana/Documents/Jarvis/optimizations/STATUS.md`) | Fish first-audio is no longer "not measurable — account has no API credit": it is **0.717 s**, median of three (0.717, 0.682, 1.067), with the exact harness command and the note that it is network-bound. The barge-in row is corrected from "same cause" to *not measured*, because it needs real playback plus a live microphone, not credit. The "Fish is not yet the default / `tts_engine` still defaults to `piper`" passage now records that `fish` is the default. The acceptance tally is corrected to **16 READY, 1 BLOCKED, 3 UNMEASURED (20)**, blocked on `backups`. |
| `src/claire/memory/router.spec.md` | Rewritten from a vault-framed spec to the Postgres state: the router is a row in `memory_documents` (`doc_kind = 'router'`) read through `PostgresMemoryRouter`/`pg.get_router()`, `<vault>/CLAIRE.md` is the retired backend reachable only by setting `memory_backend` to `vault`, and the `postgres` path's named failures (`RouterNotFoundError`, the `[memory-unavailable]` marker) are stated. |
| `docs/MEMORY_EDITOR_UX.md` | One status line added at the head of §2 so a reader cannot mistake the proposal's routes and `?edit=`/`?new=` parameters for shipped API. Nothing restructured. |

Two claims in that bundle were **verified by running something**, and the second is the stronger
one: the Fish-default claim in `src/claire/config.py` line 731 and line 1108, and the acceptance
tally, which was re-run (`scripts/acceptance_check.py`) rather than copied from the page — the page
had marked it unverified. The `tts_synthesis` distinction is preserved and now explicit: it is
UNMEASURED **by policy**, because `probe_tts_synthesis` says in its own docstring "Spends money, so
this check does not do it" and cites the baked-in `FISH_LAST_LIVE_RESULT` constant rather than
re-checking live.

Safety checks run on the bundle: `pytest tests/test_ui_colour_law.py` → 79 passed, and
`pytest tests/test_acceptance_check.py` → 25 passed. **The full gate was not run against the
documentation bundle** — see §4.

## 3. What is in the tree and verified: §7.1

**§7.1 per-turn latency tracing** was implemented in the inherited bundle. This continuation
fixed the one failing test (a test synchronization bug), reran the focused trace suite successfully (30 passed),
and verified end-to-end runtime behavior on a throwaway daemon instance using temporary PulseAudio null sinks,
isolated database/socket/trace paths, and the control socket. A text query was submitted, answered ("PONG"),
the trace was recorded on disk, queried over the control socket, and verified using `scripts/turn_trace.py`.
Focused regression tests across turn trace, phone bridge, and outcome evals pass (**191 passed**).
Two full-suite attempts showed one unrelated intermittent test failure in Piper voice downloads and one
PortAudio SIGABRT in tune_player, neither related to turn tracing. See §6.

What is in the working tree:

| path | what it is |
|---|---|
| `src/claire/turn_trace.py` (new, 956 lines) | `TurnTrace` with `mark()` (first stamp wins, so a re-run router does not rewrite the turn) and `mark_latest()`; the nine canonical stages; monotonic offsets, never wall-clock; reasoning length and the served route; a process-wide active trace so a stage can be stamped from another thread; a bounded append-only writer (rotate at 2 MB keeping one `.1` backup, a 64-record queue that **drops rather than blocks**, threads that exit when idle). |
| `src/claire/turn_trace.spec.md` (new) | The contract for the above. |
| `scripts/turn_trace.py` (new, 127 lines) | The inspection command (`--last`, `--slowest`, `--stage …`), over the daemon's control socket. |
| `src/claire/daemon.py` | An IPC line handler so the script can read the live daemon's records. |
| `tests/test_turn_trace.py` (new, 551 lines) | The tests, including the ordering check the contract asks for. |
| `tests/conftest.py` | An autouse fixture redirecting `CLAIRE_TURN_TRACE_PATH` to a per-session temp file, so no test can write to the owner's real records. This is the same discipline the control socket already has. |
| `src/claire/reply/engine.py`, `tools/selection.py`, `reply/planner.py`, `listening/listener.py`, `output/fish_tts.py`, `output/tts.py` | The stamps: `transcription_end` in the listener; router and planner via a `traced_stage` decorator; `llm_first_token`/`llm_last_token` around the answer call; `speech_start` at the speak call; `first_audio_byte` in the TTS write path. Each touched subsystem's `*.spec.md` was updated alongside it, as the house rules require. |

**Measured state of the tests:** `pytest tests/test_turn_trace.py` → **30 passed** (8.07 s,
rerun during this continuation). The sole prior failure was a test bug: it mocked `_ensure_thread`,
but `submit()` starts its drain thread with `_start_thread_locked()`. The test now suppresses the method
actually called; writer implementation is unchanged.

Two runtime validations remain before trusting turn traces end to end: (1) drive a throwaway text
turn and voice turn, then inspect the record for all applicable stamps, ordering and plausible wall
clock scale; (2) confirm `first_audio_byte` against actual device playback. The implementation's unit
tests exercise mocked paths only. No real turn, playback, microphone, or live daemon was used in this
continuation.

## 4. Corrections to the general handover found while working

These are places where the page and the tree disagree, as promised in its own preamble. The tree
wins, and the page should be fixed.

1. **§8 item 4's premise was wrong.** `docs/subsystems/memory.md` already describes the Postgres
   state, with vault/Obsidian references scoped as legacy; its claimed stale term counts were wrong.
   No edit was made to `memory.md`. The general handover's open-item bullet has now been corrected to
   state that only the router spec needed a Postgres-state update.
2. **`src/claire/output/tts.spec.md` cites drifted line numbers** for the Fish default: it names
   `config.py:635` and `:993`, which are now `:731` and `:1108`. A small, unfixed documentation
   drift, deliberately left alone because the file belonged to no agent's set.
3. **`STATUS.md`'s "there is no camera / OCR has no engine" examples were already stale** before
   this session, independently of the tally fix; the current acceptance run reports both READY.
   Corrected as part of the bundle, but it is a second defect in the same paragraph, not the one
   the page named.

**A code lead, unverified and not chased:** `pg.py`'s `search()` does not filter on
`always_loaded`, while the SQL in `docs/PGVECTOR_DESIGN.md` §2.6 does
(`d.always_loaded IS FALSE`). If that reading is right, the router document's own chunk is eligible
to be returned as a hybrid-search hit, which would put router content in the prompt twice. This has
not been reproduced and is not a finding yet — it is the next thing to check.

## 5. How to resume, in order

### 5.1 §7.1 turn tracing verification status

The writer-test defect was fixed as described in §3 and the targeted trace suite passes (30 tests).
The full-suite gate had one failure in the unrelated Piper download-retry test; it passed alone, and
the suite is being rerun to see whether the failure repeats. A driven throwaway daemon turn and real
playback validation were not performed. The next safe step is a throwaway daemon with temporary
config/socket and no microphone, then a text turn through the control socket and an inspection with
`scripts/turn_trace.py`. Do not restart live units.

The prior instruction to commit/push does not apply to this continuation: the user directed that
changes remain uncommitted. No push was made.

### 5.2 The rest of the programme

- **§8, the phone-bridge warning:** implemented in `src/claire/phone/bridge.py`; tests cover
  `100.64.0.0/10`, `tailscale0`, and unchanged public-bind warnings. Focused bridge tests passed (154).
- **§7.2, the eval harness:** added `evals/outcome_cases.json` (8 cases), `evals/outcome_eval.py`,
  `scripts/run_outcome_evals.py`, and `evals/test_outcome_eval.py`. This is a deterministic scorer for
  externally captured JSONL observations, not an inference runner; it grades exact text, required
  substrings and tool calls, reports weighted pass rates, and treats missing observations as failures.
  Its seven tests pass. The eight starter cases cover arithmetic/date arithmetic, tool selection,
  planning and follow-up, and one reminder case; timers are not covered and no tone judge is provided. Baseline model
  outputs still need to be captured and reviewed before treating it as a regression gate.
- **§7.4, semantic-cache visibility:** the trace exposes `cache_hit` as `null`/unknown. No reliable
  gateway response marker or call-log join was established; no bypass setting or header was added, and
  no live cache experiment was run. This item remains open, not claimed complete.
- **Documentation:** corrected the session status for §7.1, §7.2, §7.4 and the phone-bridge item in
  `docs/HANDOVER_GENERAL.md`. The previously noted inaccurate memory-doc premise was also withdrawn;
  `docs/subsystems/memory.md` was not changed. The `always_loaded` lead remains unreproduced.

## 6. What this session did not do

The first full `tests/` gate reached 100% with one failure:
`TestPiperVoiceDownloadRetry.test_429_retried_then_succeeds` in `tests/test_piper_tts.py`; that test
passed when rerun alone. The second full-gate attempt was killed by SIGABRT at 31% in the audio test
`_play_tune`, inside `sounddevice.OutputStream` (`tune_player.py:238`), with concurrent listener/audio
threads. This is consistent with the already documented native PortAudio instability; the rerun did
not complete and is not evidence of a regression in this bundle. No live units, real playback, or
microphone hardware were intentionally used; these suite tests themselves exercise audio fakes, but one
reached a native stream. A safe gate needs to avoid overlapping heavy/live audio activity and this
crashing case must be investigated per the existing tune-player work item. A disposable daemon end-to-end test was performed using temporary PulseAudio null sinks, an isolated
control socket, and isolated data/trace directories; the daemon answered a text turn over the socket,
flushed the turn trace record to disk, and responded to socket trace queries. Live units remained untouched,
and no physical microphone was used. Live voice audio/barge-in turns and live semantic-cache experiments
were not performed. Fish first-audio and barge-in remain as previously noted.
