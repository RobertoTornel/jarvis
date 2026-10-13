# Acceptance — the honest scoreboard for a Claire install

`scripts/acceptance_check.py` measures **every** capability Claire claims, in one
run, and prints a table of `capability / state / detail / fix`. It exists because
"the suite is green" and "this install works" are different claims: the unit
suite passes with no microphone, no camera, no OCR engine and no API credit.

```bash
.venv/bin/python scripts/acceptance_check.py            # the table
.venv/bin/python scripts/acceptance_check.py --json     # machine-readable
.venv/bin/python scripts/acceptance_check.py --only browser,ocr
.venv/bin/python scripts/acceptance_check.py --list     # capability names
```

Exit codes (never a bare traceback, never a silent green):

| Code | Meaning |
|---|---|
| `0` | every capability was measured and works |
| `1` | at least one capability is `BLOCKED` (including a check that crashed) |
| `2` | usage error — an unknown `--only` name, an empty selection |
| `3` | nothing is `BLOCKED`, but something is `UNMEASURED`: this run cannot certify the install |

## The three states

| State | Means | Rule it must obey |
|---|---|---|
| `READY` | the capability was **measured on this machine** and works | never used for something this run could not observe |
| `BLOCKED` | a named dependency, credential or engine is missing | always carries the exact command or setting that fixes it |
| `UNMEASURED` | the code is present but this machine has never observed it | never downgraded to `READY`, never hidden inside a footnote |

There is no fourth state for "probably fine". A `--json` document carries one
row per capability with `name`, `title`, `state`, `detail`, `fix`, `problem` and
`data`, plus a `summary` whose `exit_code` matches the process exit code.

## The fail-proof: one exploding check cannot take the run down

Every capability is wrapped individually, exactly like
`src/claire/platform/service.py`'s self-check. A check that raises is reported as
`BLOCKED` with `problem: true`, `data.exception`, and a fix that says this is a
defect in Claire rather than a missing package; the other nineteen capabilities
still report. `KeyboardInterrupt` / `SystemExit` are deliberately re-raised: the
owner asking to stop is not an acceptance finding.

This was written RED-then-GREEN, because a fail-proof that has never been seen
to fail is a guess. The naive first version called the probe unguarded:

```
$ .venv/bin/python -m pytest tests/test_acceptance_check.py -m unit -q -k crashing
scripts/acceptance_check.py:1392: in _run_one
    result = probe(self.context)
tests/test_acceptance_check.py:153: ValueError
E       ValueError: kaboom
2 failed, 20 deselected in 0.15s
```

Note that the `scripts/acceptance_check.py:1392` in that traceback does not
match any revision of the script on disk — in the current one `_run_one` is at
line 1477, and it was at 1465 when the scoreboard was added — so read that number
as part of the record rather than as a pointer into the current file.

That traceback is the bug the guard exists for: the owner, whose problem is
already that something is broken, gets a stack trace out of the tool that was
supposed to tell them what is wrong. After adding the guard:

```
$ .venv/bin/python -m pytest tests/test_acceptance_check.py -m unit -q
25 passed in 0.27s
```

## What is unmeasurable on this machine, and why

These are recorded in `STATUS.md` and encoded here. The script reports them as
`UNMEASURED` or `BLOCKED` — never `READY`:

| Capability | State | Why it cannot be READY here |
|---|---|---|
| `wake_word` (acoustic) | `UNMEASURED` | **No microphone audio has ever been captured on this machine.** The matcher's own text-only cost (~8 µs) is the matcher's cost, not acoustic wake latency, and is not presented as one. The in-room false-wake rate is likewise unmeasured; the harness's `0.35` "false-positive rate" is a synthetic negative set and is not a rate. |
| `tts_synthesis` (Fish) | `UNMEASURED` | The Fish key file is present (count and mode are reported; values are never read), but **the account has no API credit**: the last live call returned HTTP 402 "Insufficient API credit" (the key authenticated — a rejected credential would be 401). A live synthesis spends credit, so this check does not make one. |
| `outreach_live` | `UNMEASURED` | `dry_run` is the ladder's default and **no message has ever been delivered for real from this install**. The acceptance run never sends. |

Four capabilities that were `BLOCKED` in the recorded run below are `READY` on
this machine now — `ocr` (`tesseract` is installed), `camera` (two `/dev/video*`
devices exist), `stt_model` (`stt_model_path` points at a Parakeet `.nemo`) and
`key_store` (a key store file exists). Of that run's six `BLOCKED` rows only
`backups` and `llm_endpoint` still are, and `llm_endpoint`'s reason has changed
from "no credential configured" to a request timeout now that one is stored.
That is why this table names three capabilities rather than five: the script
reports the machine, not the run.

Barge-in latency and first-audio latency are **not** capabilities of their own in
this table — they need Fish playback, so they share the `tts_synthesis` reason.

Two capabilities deserve a note even though they are `READY`:

* `audio_device` is `READY` because an input device is enumerable. That is a
  different claim from "a microphone has ever been captured", which is why
  `wake_word` stays `UNMEASURED` next to it.
* `screen_capture` is `READY` only because a screenshot was actually taken
  during the run (mechanism and byte count are reported, then the PNG is
  deleted). A capture mechanism merely *existing* would not be enough.

## What the script deliberately does not do

* It never prints, logs or serialises a secret value. Key material appears as a
  variable **name**, a **masked** rendering (`...1234#a1b2c3d4`) or a bare
  **count**. Nothing in the script calls a raw-value accessor; the unit tests
  plant a known secret in a key store and assert it appears in neither the table
  nor the JSON.
* It does not send a message, place a call, open a port or spend API credit.
* It does not create the memory router (`create=False`), does not write the key
  store, and does not create a config file: a missing one is reported with the
  command that creates it.
* Its only filesystem side effects are one temporary PNG in the system temp
  directory (deleted in a `finally`) and whatever chromium writes in its own
  profile directory.
* It does not treat "the module imports" as "the capability works" where a real
  measurement is cheap: the calendar check performs a real clash-and-alternatives
  computation, the timer check starts/fires a timer against a fake clock, the
  browser check launches headless chromium and reads a local `data:` page back.

## Capabilities measured

`--list` prints this, and `--only` takes any of the names.

| Capability | What it measures |
|---|---|
| `config` | the config file loads; provider, voice engines and recogniser path are reported |
| `vault` | the memory vault directory exists and is writable |
| `key_store` | stored credentials, masked; credential *names* visible in `claire.env` / `channels.env` |
| `audio_device` | an input device this process can open |
| `stt_model` | the configured turn recogniser model is present |
| `wake_word` | acoustic wake behaviour — unmeasurable here (see above) |
| `tts_synthesis` | live synthesis — unmeasurable here (see above) |
| `llm_endpoint` | the configured LLM endpoint answers |
| `memory_router` | the always-loaded router is in its 6000-byte budget and re-read per request |
| `system_prompt` | the assembled prompt carries the memory section, with no problem marker |
| `calendar` | a deliberate clash is detected and ranked alternatives are proposed |
| `timers` | a timer starts, lists and fires exactly once |
| `screen_capture` | a screenshot is produced now, then deleted |
| `ocr` | text can be read off an image |
| `camera` | a camera device exists |
| `browser` | headless chromium launches and reads a page back |
| `outreach_ladder` | low/medium/high/critical classification, routing, and a provably dry run |
| `outreach_live` | a real delivery — unmeasurable here (see above) |
| `backups` | the rotating snapshot slots verify (contents re-hashed, not trusted from the manifest) |
| `service_selfcheck` | `claire.platform.service`'s own self-check runs every probe to completion |

## A recorded run on this machine (`mana-pc`, 2026-10-06T17:10Z)

`.venv/bin/python scripts/acceptance_check.py`, exit code `1`, 134 lines. The
table wraps and prints every word of each `detail` and `fix`; this is a
condensed transcription of that run, one row per capability, with the same
text. (Recorded with `COLUMNS=118`, so this is the rendering of a terminal
that wide.) The run has the `vault` memory backend selected, so its `vault` and
`memory_router` rows record that backend's output; a default install reads the
router from the Postgres store, and its `memory_router` row names the store
rather than a vault path.

```
config              READY       configuration loaded from /home/mana/.config/claire/config.json
vault               READY       /home/mana/.local/share/claire/vault (writable), router file present (914 bytes)
key_store           BLOCKED     no key store file at /home/mana/.config/claire/keys.json exists yet; names visible in claire.env: none; channels.env: DISCORD_TRANSPORT, DISCORD_SELFBOT_TOKEN, DISCORD_TARGET_USER_ID, SIGNAL_TRANSPORT
audio_device        READY       Wireless Controller: USB Audio (hw:1,0) (index 4, 1 input channel(s))
stt_model           BLOCKED     stt_model_path is empty in config.json (stt_engine=parakeet)
wake_word           UNMEASURED  no microphone audio has ever been captured; streaming model present (6 .onnx files)
tts_synthesis       UNMEASURED  1 Fish API key present (count only); last live call HTTP 402 Insufficient API credit
llm_endpoint        BLOCKED     http://127.0.0.1:20129/v1 answered HTTP 401 for /models - reachable but no credential configured (provider: openai_compatible)
memory_router       READY       router .../vault/CLAIRE.md: 804 bytes of a 6000-byte budget, re-read per request
system_prompt       READY       6678-char prompt carries the memory section, no problem marker
calendar            READY       clash detected, 402 ranked alternatives proposed (nearest +15 min)
timers              READY       60 s timer started, listed, fired once, cleared (fake clock)
screen_capture      READY       captured 295,691 bytes via pillow-imagegrab; temporary PNG deleted
ocr                 BLOCKED     the 'tesseract' binary is not installed
camera              BLOCKED     no camera found: nothing matching /dev/video*
browser             READY       headless chromium opened a local probe page and read it back
outreach_ladder     READY       urgency classified correctly; low-urgency dry run sent nothing
outreach_live       UNMEASURED  no message has ever been delivered for real from this install
backups             BLOCKED     0 of 4 snapshot slots verify under /home/mana/.local/share/claire/snapshots (the root is empty: no snapshot has been taken)
service_selfcheck   READY       7 platform probes ran; 3 reported a missing dependency, 0 crashed

Summary: 11 READY, 6 BLOCKED, 3 UNMEASURED (20 capabilities)
Problems: none - every check reported a verdict.
Verdict: 6 capability(ies) BLOCKED - fix each one above, then re-run this check.
Exit code: 1
```

### A scoreboard is a snapshot, and this machine moved during the session

Two runs ten minutes apart on this same install both printed exit `1`, and two
rows were different, because the owner was reconfiguring it while the check ran:

* `llm_endpoint` went from *"nothing answered at `http://127.0.0.1:11434`:
  Connection refused (provider: ollama)"* to *"`http://127.0.0.1:20129/v1`
  answered HTTP 401 for `/models` — reachable but no credential configured
  (provider: openai_compatible)"*. Both were true when printed.
* `backups` went from "1 of 4 snapshot slots verify" to "0 of 4" because the
  snapshot root was emptied between the runs. The backup check is provably
  read-only: its directory listing was byte-identical before and after it ran.

That is the point of the tool: re-run it after changing an install rather than
trusting a result from before the change. Nothing here is cached across runs.

That summary was the honest state of this install at the time of that run: the
memory, scheduling, senses-that-exist, browser, ladder and platform plumbing all
worked; no LLM credential was configured, `stt_model_path` was empty, the model
server was reachable only to refuse a request (`401`), there was no OCR engine
and no camera, and no backup slot verified. Read it as a snapshot, not a standing
claim — an install moves between runs, and this one has: the owner has since
stored an `OMNIROUTE_API_KEY` in the 0600 runtime env file and pointed
`stt_model_path` at a Parakeet `.nemo`, so the `llm_endpoint` and `stt_model`
rows above no longer describe it.

## Tests

`tests/test_acceptance_check.py` (unit-marked — the `-m unit` gate silently
deselects an unmarked file, and a guard test enforces the marker) locks in:

* a check that raises is reported as a problem, the run completes, and every
  other capability is still listed (both at runner level and through `main()`);
* a check returning rubbish is a reported problem too;
* an unknown check name is reported rather than silently skipped;
* `KeyboardInterrupt` still propagates;
* exit `0` when all ready, `1` when blocked, `3` when only unmeasured, `2` on an
  unknown `--only` name;
* `--json` parses and carries the same state for every capability as the table;
* `--only` filters (comma-separated and repeated) and `--list` prints every name;
* a planted secret appears in neither output, while its masked rendering does,
  and the store file is untouched;
* the script never calls the raw key accessor;
* capabilities that cannot be measured here are never `READY`;
* the browser and screen-capture checks are exercised through injected fake
  modules, so the unit tests need no real browser, screen or network.

## Adding a capability

1. Add the name to `CHECK_ORDER` and a `CHECK_TITLES` / `CHECK_SUMMARIES` entry —
   module import fails loudly if either is missing.
2. Write `probe_<name>(ctx) -> ProbeResult` using `ProbeResult.ready/blocked/
   unmeasured`, register it in `DEFAULT_PROBES`, and give every non-READY verdict
   a runnable fix (commands in this report are spelled with `PYTHONPATH=src`,
   so they run from a checkout whether or not it has been installed into the
   venv).
3. Add a unit test that injects a fake for it — never a real device.
