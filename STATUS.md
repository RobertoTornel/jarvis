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
| 1 | Voice: recognisers, transcript plumbing, Fish TTS, wake word | **in progress** (1a committed; 1b integration) |
| 2 | Memory: editable router file beside untouched RAG/graph | not started |
| 3 | Scheduling: calendar, alternative slots, reminders, timers | not started |
| 4 | Senses: screenshot/OCR, GUI automation, vision routing, browser | not started |
| 5 | Outreach: urgency ladder, Discord DM, Signal dry-run | not started |
| 6 | Platform: panel, keychain, backup/repair, systemd, Docker | not started |
| 7 | Phone: token bridge, camera, location, wire encoders | not started |
| 8 | Integration, acceptance demo, final report | not started |

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
