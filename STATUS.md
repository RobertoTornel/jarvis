# STATUS — Project Claire

Honest, current state of the build. Updated at the end of every wave. If
something here disagrees with the code, the code is right and this file is stale.

Last updated: Wave 0 (scaffold + rename), in progress.

## What Claire is

A standalone, always-on Linux voice assistant for `mana-pc`, forked from
`isair/jarvis` at commit `94c54491` (see [`ATTRIBUTION.md`](ATTRIBUTION.md)).
She runs as a plain Python application (`python -m claire`) with no harness,
Node or DSH runtime dependency.

- **Working project:** `/home/mana/Documents/jarvis_cloud`, branch `claire-rewrite`
- **Upstream reference (read-only):** `/home/mana/Documents/jarvis_real`
- **Design reference (read-only):** `/home/mana/Documents/deepseek/Claire`

## Baseline — inherited from upstream, measured on this host

Recorded **before** any Claire change, so pre-existing breakage can never be
confused with breakage Claire introduced.

| Metric | Value |
|---|---|
| Command | `.venv/bin/python -m pytest tests/ -m unit -q -p no:cacheprovider` |
| Result | **1687 passed, 1 skipped**, 1736 deselected, 3 warnings |
| Wall time | 61.85 s |
| Python | 3.12.3 (venv at `.venv`) |

The 1736 deselected tests are `integration`/`e2e`/`eval`/`performance`-marked
and are **not** part of the per-wave gate.

## Wave board

| Wave | Scope | State |
|---|---|---|
| 0 | Scaffold, rename to `claire`, delete dictation + Windows/macOS, cloud policy | **complete** |
| 1 | Voice: recognisers, transcript plumbing, Fish TTS, wake word | **complete** (1a and 1b committed) |
| 2 | Memory: editable router file beside untouched RAG/graph | **complete** |
| 3 | Scheduling: calendar, alternative slots, reminders, timers | **complete** |
| 4 | Senses: screen OCR, camera, browser control | **partial** - OCR blocked: no tesseract binary; no camera exists. Capture, browser and the honest degraded paths are done and verified. |
| 5 | Outreach: urgency ladder, Discord DM, Signal dry-run | **complete** |
| 6 | Platform: panel, keychain, backup/repair, systemd, Docker | **complete** (the panel is built; nothing is installed as a service) |
| 7 | Phone: token bridge, camera, location | **complete** (bridge + auth + loopback policy; no phone has ever connected) |
| 8 | Integration, acceptance demo, final report | **complete** |

## Wave 0 — detail

### Built

- Fork copied from `/home/mana/Documents/jarvis_real` to
  `/home/mana/Documents/jarvis_cloud` (including `.git`), branch
  `claire-rewrite` created off `94c54491`. The upstream clone was **never
  written to**.
- Baseline unit suite captured green (table above).
- Both subagent routes verified reachable through OmniRouter with no secret
  value printed: `cmd/meta/muse-spark-1.3-contributor` and
  `cmd/deepseek/deepseek-v4.1-flash`.
- Mechanical rename applied: `src/jarvis/` → `src/claire/`,
  `jarvis_desktop.spec` → `claire.spec`, 279 files rewritten
  (`jarvis`→`claire`, `Jarvis`→`Claire`, `JARVIS_`→`CLAIRE_`), with
  `isair/jarvis` upstream URLs deliberately preserved as attribution.
- New CLI entry point: `src/claire/cli.py` + `src/claire/__main__.py`
  (upstream had no `__main__`, only `-m jarvis.daemon`) and
  `pyproject.toml` with the `claire` console script.
- [`ATTRIBUTION.md`](ATTRIBUTION.md) written: fork point, licence terms, what
  was inherited as code vs. as design, what was deleted.

### Deleted (upstream components, not maintained)

- Dictation subsystem: `src/claire/dictation/`, `desktop_app/dictation_history.py`,
  `tests/test_dictation.py`, `tests/test_dictation_history.py`, its docs images,
  and its config keys/UI wiring.
- Windows/macOS packaging and CI: `installer/`, `scripts/*.bat`,
  `scripts/run_windows.ps1`, `scripts/run_macos.sh`, `src/desktop_app/macos_tray.py`
  and the macOS test/fixture files.
- `.agents/skills/triage/` — upstream-maintainer issue-triage tooling, not
  applicable to a personal fork.

### Result — Wave 0 is green

| Metric | Value |
|---|---|
| Command | `.venv/bin/python -m pytest tests/ -m unit -q -p no:cacheprovider` |
| Result | **1669 passed, 1 skipped**, 1611 deselected, 0 failed |
| Wall time | ~68 s |

The count is 18 lower than the 1687-test baseline because whole suites for
deleted components were removed, not because tests were disabled:

| Removed suite | Tests | Why |
|---|---|---|
| `test_dictation.py`, `test_dictation_history.py` | dictation gone | N6 |
| `test_macos_*.py` (5 files) | macOS out of scope | N1 |
| `test_mlx_bundle.py`, `test_mlx_bundle_integration.py`, `test_install_cuda.py` | test `installer/`, deleted with the platform packaging | N1 |

No test was skipped, marked xfail, or deleted to make the suite pass; every
remaining test runs.

### Acceptance checks

| Check | Result |
|---|---|
| Upstream unit suite green | ✅ 1669 passed, 1 skipped, 0 failed |
| Zero `jarvis` references outside attribution/licence/fork-note | ✅ only in `LICENSE`, `ATTRIBUTION.md`, `README.md`, `AGENTS.md`, `pyproject.toml`, `STATUS.md`, the `UPSTREAM_ISSUES_URL` constant in `desktop_app/app.py`, and one assertion in `test_updater.py` that upstream is *not* hardcoded |
| `python -m claire --help` runs | ✅ exit 0, prints usage and the OmniRouter/Linux description |

### Notable fixes made during Wave 0

- **`_capture_generation` had lost its only incrementer.** Deleting the
  dictation setter removed the thing that bumped it, so stale-result rejection
  had silently degraded to "not stopped". Rebound to `_clear_audio_buffers()`,
  the documented state-transition boundary. An A/B run proved the first attempt
  (bumping on every speech onset) was wrong: it broke legitimate FIFO decoding of
  consecutive utterances, failing
  `test_completed_audio_is_decoded_fifo_during_language_work` intermittently.
  The boundary version passes 5/5 in isolation and in the full suite.
- **`llm_base_url` / `llm_chat_model` defaults made provider-resolved.** Setting
  them to literal OmniRouter values made a v1 upgrade resolve to
  `provider=ollama` + an OmniRouter URL — an incoherent pair. They now default to
  empty and resolve per provider: a fresh install talks to OmniRouter, an
  existing Ollama install keeps its own endpoint.

## Measured numbers

| Measurement | Value | Method |
|---|---|---|
| Upstream unit suite baseline | 1687 passed, 1 skipped | `pytest tests/ -m unit -q` |
| Wave 0 full suite | 1669 passed, 1 skipped, 0 failed | `pytest tests/ -m unit -q` |
| Wave 1a full suite | 1864 passed, 1 skipped, 0 failed | `pytest tests/ -m unit -q` |
| Streaming model | 131 MB, `~/.local/share/claire/models/sherpa-onnx-streaming-zipformer-en-20M` | provisioned from the public k2-fsa bundle |
| Recogniser abort threshold | ~39 frames ≈ 780 ms before `is_ready()` | measured against the real library |
| Endpoint latency | **0.679 s** (stable, 3 runs) | `scripts/measure_voice_latency.py --only endpoint_latency`; 800 ms configured endpoint window, real model, real wav |
| Wake latency | *not measured yet* | needs the live microphone; the harness's text-only number (~8 µs) is the matcher's own cost, **not** acoustic wake latency, and is deliberately not presented as such |
| First-audio latency (Fish) | *not measurable — account has no API credit* | `scripts/measure_voice_latency.py --only first_audio_latency --live-fish`; a live call returned HTTP 402 "Insufficient API credit" |
| Barge-in latency | *not measurable — same cause* | needs Fish playback, which needs API credit |
| Wake false-positive rate | **0 / 26 ordinary words** wake her | guard suite over `clear, chair, declare, clue, clip, care, layer, player, clay, glare, flare, cheer, share, rare, core, score, card, car, air, lair, pair, hair, bear, pear` plus six ordinary sentences |
| Wake false-positive rate (harness) | 0.35 — **misleading, see note** | the harness's generated list is synthetic *Claire* variants (`claier`, `clire`, `lcaire`), which are intended true positives, so this number is not a false-positive rate |

### Note on the 0.35 "false-positive rate"

`scripts/measure_voice_latency.py` generates negative phrases by mutating the
wake word, so its `wake_false_positive_rate` counts genuine mishearings that
*should* wake her as if they were false positives. The number that matters is the
guard suite's: **0 of 26 ordinary English words and 0 of 6 ordinary sentences**
wake her, while every curated mishearing still does. The harness's metric needs
redefining (or splitting into "ordinary words" and "name-like variants"); it is
recorded here rather than quietly reported as a false-positive rate.

## Unproven

- No microphone audio has been captured on this machine. Every recogniser
  measurement above is from wav files, not from the live input device. The owner
  chose to leave acoustic wake latency unmeasured for now.
- **Fish Audio synthesis is blocked by the account's API credit, not by the
  code.** A live request was authorised and attempted; the API answered
  **HTTP 402 "Insufficient API credit. API credit is managed independently from
  platform credit."** The key authenticated successfully — a rejected credential
  would be 401 — so the key is good and the API credit balance is empty. This is
  a *different* balance from the platform credit shown in the Fish web UI, which
  is what made the failure look surprising. Until credit is added, first-audio
  latency and barge-in latency cannot be measured, and Claire cannot speak with
  Fish: both must report this rather than pretend.
  - The failure path behaved correctly when it happened: the client classified
    402 as a dead key, rotated the pool, reported the exact reason, redacted the
    key to last-4 plus a fingerprint, kept the voice unchanged, and invented no
    number. Exit status was non-zero.
- No Parakeet weights are present on this machine, so the authoritative engine has
  never decoded real audio. Its loaders and named failures are tested with fakes.
  Until a model is placed at `stt_model_path`, the listener degrades to the
  whisper path and reports the turn as degraded — verified directly:
  `create_stt_engine(cfg)` raises `SttModelMissingError` naming the path and the
  fix rather than returning an empty transcript.
- No Discord/Signal message has been sent from Claire.
- Barge-in has never been heard, because no audio has ever been played.

## Stale docs

None known yet — docs are updated in the same wave as the code they describe.

## Objections register

Decisions in the build brief that were implemented as specified despite a
recorded objection. Implemented as written; recorded here.

- *None yet.*

## Decisions taken during the build (not in the brief)

Recorded so they are not silently re-litigated later.

1. **Upstream auto-updater is disabled by default.** Upstream's updater polls
   the `isair/jarvis` GitHub releases and would install a different product over
   Claire. The update source is now configurable and empty by default, with an
   honest "update checks disabled" status rather than a silent upstream
   auto-update. Rationale: a fork must not overwrite itself with upstream.
2. **`isair/jarvis` URLs preserved as attribution.** The acceptance rule allows
   `jarvis` to survive in upstream attribution, and those URLs genuinely point at
   the project Claire forked from. A constant (`UPSTREAM_ISSUES_URL`) replaces the
   hardcoded issue links in the desktop app.
3. **`claire` console script added via `pyproject.toml`.** The brief names the CLI
   `claire`; upstream shipped no packaging metadata, so one was added with
   `src/claire/cli.py` as the entry point. Runtime dependencies stay in
   `requirements.txt` as the single source of truth.
4. **`.agents/skills/triage/` deleted.** It instructs triage of upstream
   `isair/jarvis` issues and belongs to the upstream maintainer's workflow, not
   to a personal fork.
5. **`omniroute` accepted as a provider alias, resolving to the
   `openai_compatible` backend.** In this codebase `llm_provider` names the
   backend *type*, not a vendor. The brief calls the provider `omniroute`, so that
   spelling is now accepted and resolved to the OmniRouter base URL instead of
   silently falling back to Ollama (which is what the old allow-list did to any
   unrecognised value).
6. **`llm_base_url` and `llm_chat_model` default to empty and resolve per
   provider.** A literal OmniRouter default made a v1 upgrade resolve to
   `provider=ollama` with an OmniRouter URL. Resolving from the effective provider
   keeps both paths coherent.
7. **Default OmniRouter chat model is `cmd/deepseek/deepseek-v4.1-flash`.** The
   previous default (`gemma4:e2b`) is an Ollama pull name and is not routable
   through OmniRouter. This id was verified reachable through the router. It is a
   provisional choice, revisitable in the panel (Wave 6).
8. **Fish Audio is wired as config surface in Wave 0 but is not yet the default
   engine.** `tts_engine` still defaults to `piper`; the Fish fields
   (`fish_voice_id`, `fish_key_file`, `fish_latency`, `fish_model`) exist and
   `"fish"` validates. The default flips in Wave 1 together with the `FishTTS`
   engine, because the current TTS factory silently falls back to Piper for an
   unknown engine name — pointing the default at a non-existent engine would be a
   silent failure.
9. **Platform-only test suites deleted with their components.** `test_mlx_bundle`,
   `test_mlx_bundle_integration` and `test_install_cuda` exercise files deleted
   under N1 (`installer/`). They assert on `installer/` paths that no longer
   exist, so they cannot be kept.
10. **`test_portaudio_serialisation.py` was retargeted, not deleted.** Its subject
    (the dictation engine) is gone, but the property it protected — PortAudio
    stream lifecycle calls serialised process-wide — is still load-bearing. It now
    tests `utils.audio_capture.open_input_stream` instead.
11. **`AGENTS.md` wake-word aliases are coherent but not final.** The mechanical
    rename left jarvis-era mishearings (`joris`, `jarvas`, …) under the new wake
    word. Wave 0 replaced them with a short Claire-only table. The full D5 work —
    measured fuzzy threshold, ordinary-word guard suite, stop-word precedence — is
    Wave 1. **Known gap:** with `wake_fuzzy_ratio` 0.78 the word "care" still
    wakes her (fuzzy 0.800). D5's guard suite must close this.
12. **`OMNIROUTE_API_KEY` is not present in this build shell.** It is injected
    into the harness environment, not exported in a login shell, so the `claire`
    process needs it supplied explicitly at runtime (systemd unit / login env).
    Claire must degrade with an honest message when it is absent — never fabricate
    a reply.
13. **`.github/FUNDING.yml` left in place.** It points at the upstream author's
    sponsorship accounts. Removing it would strip the upstream author's credit;
    keeping it is the correct behaviour for a non-commercial fork.
14. **The Fish key file was `0644` and has been tightened to `0600`.** It lives
    outside the repository (it is one of the two allowed credential files). No
    value was read, printed, or copied; only its mode changed.
15. **`wake_fuzzy_ratio` raised from 0.78 to 0.82.** 0.78 is not a safe default
    for this name: the ordinary word `care` matches `claire` at 0.800 and woke
    her. 0.82 sits above the worst ordinary word and at or below the weakest
    genuine mishearing (0.833). The alias table is a module constant
    (`CLAIRE_WAKE_ALIASES`) and `config.py` mirrors it, because a module-level
    import would cycle (`wake_detection` → `debug` → `config`). A test pins the
    duplicated values together so they cannot drift.
16. **An unknown `tts_engine` must raise, not silently become Piper.** The
    factory's fallback is a silent substitution — the user asks for one voice and
    gets another. This is also why the Fish default is flipped in the same wave as
    the engine rather than before it.
17. **The thinking-tune fallback spinner moved from stdout to stderr.** It is
    decorative, and stdout is the data channel: on a machine with no usable audio
    device the fallback runs and its spinner corrupted
    `measure_voice_latency.py --json`. Generalised rule for later waves: nothing
    decorative may write to stdout.
18. **`finalize()` feeds 500 ms of tail padding before flushing.** Without it a
    segment that ends on speech never reaches an endpoint and drops its last
    words. Measured on the real model: with the padding the reference utterance
    transcribes; the padding is silence, so it cannot add spurious words.
19. **The streaming model is provisioned into the data dir, not referenced from
    the read-only reference checkout.** `~/.local/share/claire/models/…` now holds
    a copy of the public k2-fsa bundle so Claire does not depend on another
    project's directory layout. A downloader so a fresh machine can fetch it is
    part of Wave 1b.
20. **The listener degrades to whisper when no Parakeet model is configured, and
    says so.** No Parakeet weights exist on this machine, so the alternative would
    be either a crash at startup or a turn silently labelled as Parakeet. The turn
    is marked degraded with a reason naming the missing setting, logged once.
21. **The owner's Fish voice id lives in the runtime config, not in the repo.**
    The cloned voice is a personal asset; hardcoding it as a repository default
    would put it in every checkout. It is set in `~/.config/claire/config.json`
    (0600, outside the repo) and the repository default stays `None`, which must
    fail with a named error rather than silently pick some other voice.
22. **A live Fish call is gated behind an explicit `--live-fish` flag.** A real
    request spends the owner's API credit, so the latency harness will not make
    one by accident: without the flag it reports the measurement as not taken and
    exits non-zero. This is the same reasoning as the hard stop on spending money,
    turned into a mechanism rather than a promise.
23. **The thinking-tune spinner's home is stderr, and that is now a rule.**
    See item 17; recorded separately because it is the general case. Any future
    decorative or progress output goes to stderr, so stdout stays parseable.
24. **Memory is a live re-read of one router file, not a conversation log.**
    `CLAIRE.md` in the vault is re-read from disk on *every* turn and rendered
    into the system prompt. Consequences, verified end to end rather than
    assumed: editing the file changes her behaviour with no restart, and memory
    costs a constant number of tokens per turn instead of growing the history.
    The alternative — appending the file as a chat message each turn — is what
    makes context rot unbounded.
25. **The router file is capped at 6000 bytes and warns loudly when exceeded.**
    Measured against a 16 259-byte file: the render truncates at a line boundary
    and emits `Real size: 16210 bytes. Budget: 6000 bytes. Dropped: 10260
    bytes.`, and the system prompt adds a `[memory-over-budget]` marker telling
    her to move detail into notes. Silent truncation would be the failure mode;
    visible rot is self-correcting.
26. **The router is a router, not a database.** A fact bigger than one line
    belongs in a note file with only a pointer left in the main file, and
    `unreferenced_notes()` reports notes the router never mentions so the pointer
    set can be kept honest.
27. **Frontmatter is parsed without adding PyYAML.** The needed subset is flat
    `key: value` lines; a dependency for that would be disproportionate. A
    missing or malformed block falls back to defaults rather than failing.
28. **The router decodes strictly, unlike the note reader.** The note reader uses
    `errors="replace"`, which is right for a note but wrong for always-loaded
    memory: a lossy decode would silently drop part of her memory. An undecodable
    router raises a named error instead.
29. **The router is loaded at the one call site that assembles the persona
    prompt.** The seam shipped able to accept a router but with no caller passing
    one, which would have meant memory never reaching the model at all — a
    reminder that a tested seam is not the same as a wired feature. Verified by
    rendering the real prompt: 5 627 bytes without memory, 6 705 with the vault
    router present.

## CORRECTION: the gate was silently skipping our own tests

This is the most important entry in this file, because it invalidated a green
result that had already been reported several times.

The gate is `pytest tests/ -m unit`. A test file with **no unit marker is
deselected, not failed**. Six of the seventeen test files added in Waves 1–3
carried no marker, so **148 of our own tests had never once executed** under the
gate — including `test_transcript_divergence.py`, the suite that guards the
single most important behaviour in the voice pipeline (that the provisional
recogniser can never authorise an action on its own). The suite reported green
throughout, and green meant nothing about those files.

Worse, the same hole swallowed the whole of Wave 3: 388 of the 426 schedule tests
were deselected. The agents' own reports were accurate — the tests passed when run
directly — which is exactly why the gap was invisible.

Fixed by marking every file we added, and closed permanently by
`tests/test_test_files_are_marked.py`, which records the 60 pre-existing upstream
unmarked files and fails if any *new* unmarked file appears, naming it and saying
what to do. A second assertion fails if a recorded file later gains a marker, so
the record cannot rot in the quiet direction either.

Corrected numbers: the gate now runs **2572 passed, 1 skipped** (1 620 deselected
are upstream files plus the mutable integration/e2e/performance suites). The
earlier figures in this file — 1 864, 1 941, 1 996 — were real but understated,
and the increment between them understated how much was actually verified. The
lesson recorded for future waves: after adding tests, prove the gate *ran* them
(compare the deselected count), not merely that it was green.
30. **Overlap is half-open, and touching ranges are not a conflict.** An event
    ending 15:00 and one starting 15:00 do not collide; 15:59 does. Verified
    directly: `ranges_overlap(900, 960, 960, 1020)` is `False`. This is worth a
    numbered decision because the opposite choice is the intuitive one and it
    silently refuses correct bookings.
31. **A move excludes the event being moved from its own conflict check.**
    Otherwise the event conflicts with itself and every legitimate move is
    refused. Verified: moving the Dentist from 15:00 to 15:15 succeeds with zero
    self-conflicts.
32. **On conflict nothing is written, and the refusal carries the alternatives.**
    Measured: a 60-minute "Haircut" at 15:30 against a 15:00 Dentist returns
    `ok=False`, the message `'Haircut' clashes with 1 existing occurrence(s);
    nothing was written`, and 360 ranked free slots — nearest first, starting at
    16:00. The tool then speaks only the best 3, on the reasoning that more than
    three is not a choice.
33. **Proposals must fit inside the allowed window and horizon, checked over the
    whole span.** A scan that checked only the start would propose a slot
    overrunning the 21:00 window end, or a "nearest alternative" 400 days out,
    which is not an alternative but a different plan.
34. **Timers are not calendar events, and do not survive a restart.** Modelling a
    25-minute timer as an appointment would be wrong, and claiming it persists
    would be a lie about behaviour, so the code says so instead.
35. **The unit gate must be proven to have run a new test, not assumed.** See the
    correction above. Green is only meaningful once the deselected count shows the
    new tests were collected.

## Wave 4: senses, and a credential leak found by verification

What works on this machine, measured rather than assumed:

| Capability | State |
|---|---|
| Screen capture | **works** — Pillow `ImageGrab`, 265–284 KB PNG at 1920×1080 |
| Screen OCR | **blocked** — the `tesseract` BINARY is absent; `pytesseract` is only a wrapper |
| Browser control | **works** — real chromium 1243, rendering and clicks verified live |
| Camera | **absent** — no `/dev/video*`; reported as a missing capability |

OCR fails with a named error carrying the exact fix, because pytesseract's own
message does not:
`OCR engine missing: the 'tesseract' binary is not installed on linuxmint… Fix:
sudo apt-get install -y tesseract-ocr`.

A defect inherited from upstream was found on the way: `tools/builtin/screenshot.py`
is **macOS-only** (`screencapture`) and returns `""` on Linux — a silent empty
result, the failure mode this project keeps finding.

### The credential leak, and why heuristics were the wrong foundation

Independent verification of the browser module reproduced a **real credential
leak** that the authoring agent had reported as tested and safe:

```
s.fill('#pw', 'Sup3rSecretValue!42')
  value_redacted -> 'Sup3rSecretValue!42'      # plaintext, in the result AND the log
  looks_like_secret(...) -> False
```

Two causes: `_SECRET_FIELD_PATTERN` matched `pwd` and `pass…` but **not `pw`**, one
of the most common password-input ids there is; and the value heuristic missed a
strong password because it required a pure alphanumeric run.

The fix is not a longer word list, because a word list is a guess. The order of
authority is now:

1. **DOM truth** — ask the browser the element's `type`. If it is `password`, the
   value is a secret regardless of the selector and regardless of what it looks
   like. Guessing is not involved.
2. **Fail closed** — if the type cannot be determined, redact. The code must be
   able to *prove* a value is safe before showing it, never the reverse.
3. A `pw` pattern with a proper boundary (`#pw` yes; `pwned` and `power` no).
4. The value heuristic, demoted explicitly to a backstop.

Re-verified live after the fix: `value_redacted='[REDACTED]'`, `secret=True`, and
the secret appears in neither `repr(result)` nor the captured log. Fail-closed
paths are asserted by their own tests.

The lesson is recorded because it generalises: the agent's report was confident,
specific and wrong. Verification against the running system is what found it, and
"blocked by a heuristic" is not the same as "safe".

## Wave 5: outreach

The ladder was implemented against a shared interface pinned in the briefs, and
the transports landed independently as intended. Verified end to end with
injected fakes rather than trusting the reports:

| Check | Result |
|---|---|
| `low` keeps it in the conversation | nothing sent, reported explicitly |
| `medium` / `high` | one Signal text; `high` retries a failure |
| `critical` | attempts a call and reports whether it was **really placed** |
| Discord while Signal works | **not contacted at all** |
| Signal fails | Discord used as last resort, after the preferred channel |
| Dry run | the default, and it makes no network call |

Discord is never first. That is asserted rather than assumed, because a
last-resort channel that gets used first is worse than no channel.

### A latent trap found by integration, not by the unit tests

`can_place_call()` recognised `call`, `can_call`, `supports_call` and a `*_call`
name — but **not `place_call`**, the method name this project's own shared
interface documents. Nothing broke, because the real Signal transport happens to
declare the capability three other ways as well. A transport implementing only
`place_call` would have been *silently skipped at `critical`* — the one level
where the call is the entire point.

The unit tests did not catch it because every fake they used happened to define
one of the recognised spellings. The integration check caught it by asking the
real class a question the fakes had never been asked.

Fixed additively, with a fail-proof that was actually run: neutralising the fix
turns the new test RED (`test_place_call_only_transport_is_call_capable` fails),
restoring it turns it GREEN. Discord remains call-incapable no matter how it
declares itself, including if it grows a `call()` method.

## Wave 6: platform

| Piece | State |
|---|---|
| Key store | 0600, outside the repo, masked as `...3344#48d1e459` |
| Backup | 4 rotating slots, SHA-256 verified |
| Service / self-check | per-probe isolation, so one broken probe cannot crash the report |
| systemd unit, Dockerfile | written; not installed, not built |

Verified live rather than taken on report:

- **One flipped byte in a backup was caught, and the size was unchanged** —
  `1 changed; … content changed (sha256 eaad… != manifest 33a7…, size 17 vs 17)`.
  A size-only check would have passed it, which is exactly why the manifest hashes.
  `restore()` of that snapshot was **refused** by name.
- A key store file at `0644` was **tightened to `0600` and the tightening was
  reported**: `key store permissions were too loose and have been tightened`.
- `describe()` returns `masked='...3344#48d1e459'`; the raw value appears in
  neither `repr()` nor the description; a missing name raises `KeyNotFoundError`
  naming the key and the store file, never a value.

One self-inflicted incident, recorded because it is the kind of thing that must
be disclosed: while verifying the key store I wrote a test key at the file's
**real** location, because the store honours `$CLAIRE_KEY_STORE` and I had set
`CLAIRE_CONFIG_DIR`. The store had no owner data in it, the file contained only
my own test key, and it was removed immediately — but the mistake was writing to
a real path from a verification script, and it is noted rather than quietly
cleaned up.

## Wave 7: phone bridge

A token-authenticated HTTP bridge so the phone can send text, photos and
location, and collect her replies.

- **Authentication is mandatory on every route, including `/health`**, and the
  comparison is `hmac.compare_digest`, never `==` — which leaks timing. Verified
  by reading the source for `==`: there is none in the comparison.
- A missing token, a wrong token, and a wrong token **of the same length** all get
  byte-identical 401s and none of them reaches the handler.
- **Loopback by default, and refusing anything else is a real guard, not a
  comment.** Verified: `0.0.0.0` and `192.168.1.50` are refused with
  `PublicBindRefusedError`; an empty host falls back to `127.0.0.1`.
- A photo is stored and handed to the vision seam; with no vision model the
  response says so rather than inventing a description.

**No phone has ever connected.** The bridge is tested with injected collaborators
and no socket; the wire protocol has never carried a real request from a real
handset.

## Wave 8: the acceptance scoreboard

`scripts/acceptance_check.py` runs every capability and reports READY / BLOCKED /
UNMEASURED with a fix for each, exiting non-zero if anything is blocked. Every
probe is individually guarded, so one exploding probe is reported as a problem
instead of taking the report down.

The recorded run on this machine: **11 READY, 6 BLOCKED, 3 UNMEASURED.**

The design rule that makes it worth having is that it must not overclaim. A
capability that cannot be measured here is reported as unmeasurable **with the
reason**, never as ready: no microphone has ever captured audio, the Fish account
has no API credit so live speech cannot run, OCR has no engine, and there is no
camera.

## Two integration faults found at the end, both real

Neither was found by a unit test. Both were found by running the finished system
and reading what it said.

**1. The install was pointed at Ollama, against N4.** `llm_provider` was
`"ollama"` and `llm_base_url` was unset, so the endpoint check probed
`127.0.0.1:11434` — a server that is not running. N4 requires everything to go
through OmniRouter. The config had become this way through an upstream migration
that deliberately stamps existing installs as `ollama` to preserve their old
behaviour, which was right for upstream and wrong here. The install is now set to
`omniroute` with `http://127.0.0.1:20129/v1`.

**2. A 401 was diagnosed as the wrong fault.** With the endpoint corrected,
OmniRouter answered **401** — it is running and simply has no key configured. The
check reported "reachable but not serving an OpenAI-compatible model list" and
told the owner to check `llm_base_url` and the server log, sending them after a
URL problem that did not exist. A refused credential and a malformed URL need
different fixes, so they now produce different messages: a 401/403 is reported as
a credential problem naming `OMNIROUTE_API_KEY`, and a 500 stays a server fault.
A test pins the distinction in both directions.

**The honest state of the LLM half: reachable, unauthenticated.** OmniRouter is up
on 20129 and `OMNIROUTE_API_KEY` is not set anywhere on this machine, so Claire
cannot complete a turn. That is the single blocking item for a working
conversation, and it is a missing credential rather than a code fault.

### Cleanup disclosed

Verifying the backup system left two snapshots and one deliberately corrupted byte
in the real store at `~/.local/share/claire/snapshots`, and the acceptance run then
dutifully reported backups as BLOCKED because of damage I had caused. No owner
data was involved — the payloads were my own temporary test files — and both
snapshots were removed. Like the earlier key-store incident, a verification script
wrote to a real path, and that is recorded rather than quietly tidied away.
