# Claire: session handover, 2026-10-14

**This page records one working session's progress against the next-work programme in
[`HANDOVER_GENERAL.md`](HANDOVER_GENERAL.md) §7, and it is deliberately blunt about what is
verified and what is not.** The general handover remains the description of the system; this page
is the state of the work. Where the two disagree, the general handover is right about the system
and this page is right about the work.

- **Repository:** `/home/mana/Documents/jarvis_cloud`, HEAD `62f814b` on `main` when this page was
  written. Nothing from this session was pushed.
- **Working tree: dirty on purpose.** 18 modified files and 4 new ones, all uncommitted. They are
  described below and must be committed (or reverted) as a unit, not file by file.
- **The live system was never touched.** `claire`, `claire-panel` and `claire-desktop` kept running
  throughout; no window was opened, no audio device was used, and the owner's real
  `~/.local/share/claire/turns.jsonl` has never been written by a test.

---

## 1. What was asked

Take over per `HANDOVER_GENERAL.md` §10 and work the §7 programme in the written order — §7.1 turn
tracing first, then §7.2 (eval harness), then §7.4 (semantic-cache visibility); §7.3 is a documented
negative result and is **not** to be rebuilt — plus the §8 small well-scoped fixes. Subagents do the
work; the orchestrator commits, runs the full gate serially, and reviews every diff rather than
trusting a report.

## 2. What is finished

### 2.1 The general handover itself is committed

The 2026-10-14 revision of `HANDOVER_GENERAL.md` existed only as uncommitted working-tree content —
the committed copy was the older 2026-10-13 page. Committed as `62f814b` before any other work, so
the next reader gets the current page from a clean checkout.

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

## 3. What is in the tree and NOT verified: §7.1

**§7.1 per-turn latency tracing was implemented by a subagent that then failed and left no closing
message.** Its gate logs went with it (`/tmp/gate_run2.txt` no longer exists), so **there is no
evidence that the full suite passes against this work** — only that the subagent was on its second
gate run when it died. Treat everything in this section as an unverified lead until the gate is
re-run.

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

**Measured state of the tests:** `pytest tests/test_turn_trace.py` → **29 passed, 1 failed** (8.5 s).
The failure is
`TestWriter::test_submitting_to_a_full_queue_drops_rather_than_blocks`: after `_WRITE_QUEUE_MAX`
submissions with the drain thread suppressed, the test expects `writer._q.full()` and gets `False`.
Either the queue is sized beyond `_WRITE_QUEUE_MAX`, or `__init__` starts the drain thread so the
queue is being consumed under the test — the two candidates are a code bug and a test bug
respectively, and which one it is has not been established. **This is the first thing to fix.**

Two things a reviewer should check before trusting the whole design, because the subagent died
before anyone re-read them critically:

1. **That the stamps land on a real turn at all.** Every stage is wired somewhere, but "wired" is
   not "stamped on the voice path". This needs one driven turn (§5.1) before the record can be
   believed.
2. **That `first_audio_byte` is what its name claims** — the first byte written to the device, not
   the moment synthesis was requested. The `turn_trace.py` comment at line 42 to 47 says the
   listener's `speech_start` and the TTS worker's `first_audio_byte` are ordered as they actually
   happen rather than as the general handover lists them, which is a reasonable reading and a
   deliberate deviation from the page's ordering. It should be confirmed against real playback.

## 4. Corrections to the general handover found while working

These are places where the page and the tree disagree, as promised in its own preamble. The tree
wins, and the page should be fixed.

1. **§8 item 4's premise is wrong.** `docs/subsystems/memory.md` is **already** in the Postgres
   state — it opens by placing the router and the notes in PostgreSQL and scopes every `vault`,
   `obsidian` and `CLAIRE.md` mention as legacy, and it was last changed by the migration commit
   `1dbede9` itself. The page's term counts for it are also wrong: the tree has 50 occurrences of
   "vault" (case-insensitive 55) and 14 of "obsidian", not 30 and 9. For
   `src/claire/memory/router.spec.md` the page says 4 "vault"; the tree had 5. **No edit was made**
   to `memory.md`, which is the correct outcome, and the page's item should be withdrawn.
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

### 5.1 Finish and verify §7.1

```bash
cd /home/mana/Documents/jarvis_cloud
# 1. Fix the failing writer test; decide bug-vs-test first, do not paper over it.
env -u DISPLAY nice -n 19 .venv/bin/python -m pytest tests/test_turn_trace.py -q -p no:cacheprovider

# 2. Drive one turn and confirm the stages are present, ordered and plausible.
#    Boot a throwaway daemon with no microphone and no display — the rig in
#    HANDOVER_GENERAL.md §3 (null sinks, temp config, temp socket, CLAIRE_PHONE_BRIDGE=0),
#    then drive it over the control socket and read the record back with
#    scripts/turn_trace.py. Do NOT restart the live units for this.

# 3. Only then, the gate — one at a time, nothing else heavy running:
env -u DISPLAY nice -n 19 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider
#    Baseline at this snapshot: ~6148 passed, 3 skipped, 2 deselected, 0 failed.
```

Then commit the documentation bundle and the §7.1 work as **two** commits (they are unrelated
changes), and push to `personal`. Never to `origin`.

### 5.2 The rest of the programme

- **§8, the phone-bridge warning** (`src/claire/phone/bridge.py`): a real code task with tests,
  still untouched. Make the non-loopback warning distinguish a Tailscale bind (`100.64.0.0/10`, or
  bound on `tailscale0`) from a genuinely public one, and keep the loud warning for the rest.
- **§7.2, the eval harness** (`evals/`, 44 modules, `scripts/run_evals.sh`): untouched. The
  existing suite is a two-model comparison dated 2026-05-04, defaults to local Ollama models with an
  LLM judge, and is not a graded regression gate. What is wanted is a small graded set by
  observable outcome, pass rates rather than latency, an LLM judge only for tone.
- **§7.4, semantic-cache visibility**: genuinely gated on §7.1, because the `cache_hit` field
  belongs in the trace. The gateway's response-side marker was not found, so the honest field
  default is "unknown" until a marker or a join key exists — the trace must not fake it.
- **The three page corrections in §4 above**, and the `always_loaded` lead, which needs
  reproduction before it is called a bug.

## 6. What this session did not do

It did not run the full gate against any change. It did not touch the live units. It did not
re-measure Fish first-audio latency live (the 0.717 s figure is quoted from the recorded run). It
did not verify audio playback end to end, and barge-in has still never been heard on this machine.
