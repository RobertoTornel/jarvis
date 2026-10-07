# Claire — handoff, 2026-10-07 ~01:00

Written because four subagents were in flight and two were duplicates. Read this before
trusting any agent's summary, including mine.

## State of the tree

- Branch `claire-rewrite`, uncommitted working changes across `src/claire/`, `android/`, `tests/`.
- Gate as last measured by the panel agent: **3498 passed, 1 skipped, 0 failed**.
  Writers were still active after that run, so **re-run it before believing it**:

      cd /home/mana/Documents/jarvis_cloud
      .venv/bin/python -m pytest tests/ -m unit -q -p no:cacheprovider

  A test file without `pytestmark = pytest.mark.unit` is silently DESELECTED, not failed.

## Duplicate subagents — kill these two

| id | job | verdict |
| --- | --- | --- |
| `f4d91e33` | android + six phone defects | **KILL** — duplicate |
| `9fb12c64` | whisper removal (retry) | **KILL** — duplicate, my error |
| `87bbabee` | android rework | keep — owns the app |
| `da232ac2` | whisper removal (original) | keep — already did the work |

I spawned the two duplicates because the harness reported `87bbabee` and `ddeeefa9` as
"stopped before finishing" when they were **not** stopped. Subagents cannot be reached by
`send_message` or `interrupt_agent`; only `spawn_teammate` teammates can. `claire_halt`
interrupts *all* subagents, so it over-kills — do not use it to drop one.

**Next time: call `claire_graph` before respawning anything.** A "stopped" notification is
not evidence that work stopped.

## What is actually finished

- **Panel** (`bd47a7e4`, complete). Design system, chat, transcript. Zero JavaScript.
  3498 green. `tests/test_panel_server.py` byte-for-byte unmodified.
- **Whisper removal** — in the tree from `da232ac2`: `_configures_whisper()`,
  `_initialise_whisper_backend()`, `_decode_utterance()` no-fallback logic, `_run` gate.
  **Not yet verified**: the running daemon still logged Whisper at last check. Verify with
  `journalctl --user -u claire -b | grep -ci whisper` → must be 0.
- **Android rework** — APK built (7.1 MB), manifest declares
  `foregroundServiceType="microphone|location|camera"`, `ACCESS_BACKGROUND_LOCATION`,
  `FOREGROUND_SERVICE_CAMERA`, `FOREGROUND_SERVICE_LOCATION`,
  `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`, `SYSTEM_ALERT_WINDOW`.
  **Never verified on device.**

## What is NOT fixed

**The feedback loop — the worst bug.** `src/claire/daemon.py` still contains **zero**
wake-word references. `_phone_utterance_sink` sends every transcribed utterance to the
reply engine, so the phone mic hears Claire's own voice and she answers herself forever.
The guard already exists and is simply unused: `daemon.py:63 is_speaking()` and
`tts._is_speaking`. Reuse `is_wake_word_detected` from
`src/claire/listening/wake_detection.py:140` — do not write a second matcher.

Also outstanding: the six phone defects in full, and the docs subagent (deferred until all
writers stop).

## OmniiRoute — fixed tonight

`omniroute.service` was in a crash-restart loop: a **stray OmniiRoute (pid 9681) held port
20129**, so the unit could never bind and retried every 5 s (`NRestarts=281`). Stopped the
unit, killed the stray after confirming its identity via `/proc/<pid>/cmdline`, restarted.

Now: systemd-owned (`MainPID=35818`), listening on `0.0.0.0:20129`, serving live requests.

## The latency answer — the owner was right

Journal evidence that the ~60 s round trip is **provider rate limiting, not our code**:

    [RATE-LIMIT] cid=... provider=command-code remainingForQueue=120000ms
    [STREAM] COMMAND-CODE | deepseek/deepseek-v4.1-flash | 12240ms | complete

Routing: `cmd/deepseek/deepseek-v4.1-flash → command-code/deepseek/deepseek-v4.1-flash`.
Nothing to fix locally.

## Ports

| port | what |
| --- | --- |
| `127.0.0.1:8765` | panel |
| `0.0.0.0:20129` | OmniiRoute |
| `100.104.1.69:8788` | phone bridge (inside the daemon) |
| `127.0.0.1:3080` | DSH GUI |

## Tomorrow

1. Kill the two duplicates. Confirm writers are quiet (`find src tests android/app/src -newermt "2 minutes ago"`).
2. Land the wake gating. **Reproduce the loop first**, then prove it gone.
3. Verify Whisper is actually gone from the journal.
4. Build, install and drive the APK on the phone.
5. `systemctl --user restart claire` after daemon changes — the bridge lives inside it.
6. Then spawn the docs subagent.

## Never forgotten

- Reboot validates the boot ordering (`omniroute` → `claire` → bridge retry). Not yet tested.
- The bridge is **plain HTTP over the tailnet**. Prefer `ssh -L 8788:127.0.0.1:8788`.
- Owner actions pending: change the sudo password, rotate the OmniiRoute key and the Discord token.
- `src/claire/utils/redact.py` cannot match `sk-proj-…` keys — a real scrubber gap.
- There is **no durable conversation transcript** anywhere; the daemon holds dialogue in
  memory and nothing writes utterances to disk. A panel transcript would need a daemon change.
- Never print, log or commit a secret.
