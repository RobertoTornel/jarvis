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
| 1 | Voice: recognisers, transcript plumbing, Fish TTS, wake word | not started |
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
| Wake latency | *not measured yet* | — |
| Endpoint latency | *not measured yet* | — |
| First-audio latency (Fish) | *not measured yet* | — |
| Barge-in latency | *not measured yet* | — |
| Wake false-positive rate | *not measured yet* | — |

## Unproven

- Nothing has been exercised on live audio yet; the voice pipeline is Wave 1.
- No live Fish Audio synthesis call has been made from Claire.
- No Discord/signal message has been sent from Claire.

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
