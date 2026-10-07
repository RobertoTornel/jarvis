# Deployment and startup

**Authoritative spec:** [`src/claire/platform/service.spec.md`](../../src/claire/platform/service.spec.md)
(the health/self-check contract and the facts the systemd unit must have).
Read this when you are changing how Claire is started or supervised — the
systemd user unit, the one-command `start.sh`, the console entry points, the
daemon's startup order, the environment it reads, or where its output goes.

The shipped artifacts are themselves authoritative and are quoted below:
[`deploy/claire.service`](../../deploy/claire.service) and
[`start.sh`](../../start.sh). Specs next to the code win over this document;
where this page and the code disagree, the code decides. Contradictions found
while writing this page are listed under
[Documentation drift](#documentation-drift).

## Responsibility

This subsystem owns the path from a user command to a running daemon:

- the console entry points (`python -m claire`, the `claire` script) and their
  arguments;
- process supervision on Linux: the systemd **user** unit and the one-command
  `start.sh` that renders it, creates the virtualenv, and starts the components;
- the daemon's boot order — config and database, MCP discovery, dialogue memory,
  location, TTS, the voice listener, the phone bridge, the feedback worker, and
  the main loop — and its shutdown order;
- the environment the daemon reads and the files it writes;
- the container image, which runs the text/tool half only.

It does **not** own the listening, reply, memory or MCP internals; it starts
them. The MCP boot step is documented in [`mcp.md`](mcp.md).

Plainly: **the systemd unit starts the daemon only.** The web panel
(`python -m claire.panel.server`) and the desktop app
(`scripts/run_desktop_app.sh`) are separate processes and are not in the unit.
The phone bridge is started by the daemon itself (`daemon.py:1581`), which is why
`start.sh` must not start it too.

## Entry points

| Command | Lands at | Notes |
|---|---|---|
| `claire` (console script) | `pyproject.toml:29` → `claire.cli:main` | Installed by `pip install -e .` or a wheel; in a bare checkout the unit does not use it. |
| `python -m claire` | `src/claire/__main__.py:3` → `claire.cli:main` | What the unit's `ExecStart=` runs (unit:77) and what `start.sh` spawns (`-m claire`). |
| `python -m claire.main` | `src/claire/main.py:9` → `claire.daemon:main` | Legacy/desktop path; the desktop app spawns it (`src/desktop_app/app.py:2515`). |
| `python -m claire.daemon` | `src/claire/daemon.py:1786`–`:1789` | Reads `--smoke-test` straight from `sys.argv`. |
| `python -m claire.panel.server` | `src/claire/panel/server.py:4114` (`main`) | The web panel; not part of the unit. |
| `python -m claire.platform.service --self-check` | `src/claire/platform/service.py:1249` (`main`), `--self-check` at `:1217` | Health report used by `start.sh check`, the unit's `Documentation=` target, and the Dockerfile `HEALTHCHECK`. |

`claire.cli` accepts exactly `--smoke-test` (`cli.py:19`–`:23`) and `--version`
(`cli.py:24`–`:28`), then hands off to `daemon.main(smoke_test=...)`
(`cli.py:49`–`:51`). `--smoke-test` initialises every subsystem, prints
`SMOKE_TEST_INIT_OK`, tears the subsystems down and returns without entering the
loop (`daemon.py:1583`–`:1622`).

## Key artifacts and code anchors

### `deploy/claire.service` — a systemd **user** unit, shipped as a template

The file is a template: it contains `@CLAIRE_REPO_ROOT@` where the checkout path
belongs and refuses to be used unrendered (`claire.service:3`–`:12`). The
renderer is `start.sh --install-service` (`start.sh:769`–`:829`), whose
`render_unit` helper substitutes the placeholder in pure bash so a `&`, `/` or
`\` in the checkout path stays literal (`start.sh:762`–`:767`), then reloads
systemd (`start.sh:821`–`:822`). The unit file spells out why it is a user unit
and not a system unit (`claire.service:18`–`:34`).

Exact directives (current template, paired with their symbol):

| Directive | Value | Line |
|---|---|---|
| `[Unit] Description=` | `Claire - always-on Linux voice assistant` | `:46` |
| `Documentation=` | `file://@CLAIRE_REPO_ROOT@/src/claire/platform/service.spec.md` | `:47` |
| `After=` | `graphical-session.target pipewire.service wireplumber.service network-online.target` | `:50` |
| `Wants=` / `PartOf=` | `network-online.target` / `graphical-session.target` | `:51`–`:52` |
| `StartLimitIntervalSec=` / `StartLimitBurst=` | `300` / `5` | `:56`–`:57` |
| `[Service] Type=` | `simple` | `:60` |
| `WorkingDirectory=` | `@CLAIRE_REPO_ROOT@` | `:64` |
| `Environment=PYTHONPATH=` | `@CLAIRE_REPO_ROOT@/src` | `:68` |
| `Environment=PYTHONUNBUFFERED=` | `1` | `:70` |
| `EnvironmentFile=` | `-%h/.config/claire/claire.env` (leading `-` = optional) | `:76` |
| `ExecStart=` | `@CLAIRE_REPO_ROOT@/.venv/bin/python -m claire` | `:77` |
| `KillSignal=` | `SIGINT` | `:80` |
| `TimeoutStopSec=` | `20` | `:81` |
| `Restart=` / `RestartSec=` | `on-failure` / `5s` | `:85`–`:86` |
| `StandardOutput=` / `StandardError=` / `SyslogIdentifier=` | `journal` / `journal` / `claire` | `:87`–`:89` |
| Hardening | `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=strict`, `ProtectHome=read-only` | `:93`–`:96` |
| `ReadWritePaths=` | `%h/.local/share/claire %h/.config/claire %h/.cache/claire %h/.cache/huggingface %h/.cache/numba` | `:109` |
| `Environment=NUMBA_CACHE_DIR=` | `%h/.cache/numba` | `:124` |
| `[Install] WantedBy=` | `default.target` | `:129` |

Keys are **not** in this file: `EnvironmentFile=` points at a 0600 env file and
there is no inline `Environment=...KEY=...`. The file says so twice, in its own
"KEYS ARE NOT IN THIS FILE" block (`claire.service:37`–`:43`) and next to the
directive (`:71`–`:76`).

### `start.sh` — the single startup script

`start.sh` is the one-command entry point ("Claire - one command that brings the
whole system up, and takes it down again", `start.sh:3`). It locates the repo
from its own path (`start.sh:56`–`:62`), derives every default (`:64`–`:88`), and
dispatches one of `start`, `stop`, `restart`, `status`, `check` or
`install-service` (`:902`–`:910`).

What it does on `start` (`command_start`, `start.sh:679`–`:732`), in order:

1. Resolve the interpreter (`resolve_pybin`, `:196`–`:204`).
2. Preflight checks (`hard_checks`, `:540`–`:607`), including "is this a Claire
   checkout", venv presence, `requirements.txt`, writability of the config dir,
   and whether the runtime env file exists.
3. Create `.venv` if missing (`ensure_venv`, `:210`–`:242`) and install
   `requirements.txt` when its hash changed, without touching a venv a live
   Claire runs from (`ensure_deps`, `:252`–`:291`, hash at `:206`–`:208`,
   live-venv guard at `:244`–`:250`).
4. Run Claire's own self-check (`self_check`, `:608`), and stop on failure only
   with `--strict` (`:700`–`:706`).
5. Start the daemon (`start_daemon`, `:366`–`:431`): through `systemctl --user
   start claire` when the unit is installed, otherwise as a detached process
   whose pid file is trusted only after `/proc` confirms the command line
   (`_spawn`, `:349`–`:364`; pid check, `:108`–`:129`).
6. Start the web panel (`start_panel`, `:432`–`:474`) on the panel's own
   `DEFAULT_BIND_HOST` / `DEFAULT_PORT` read out of `panel/server.py`
   (`panel_default`, `:328`–`:331`; defaults are `127.0.0.1` and `8765`,
   `src/claire/panel/server.py:149`–`:150`). If something already listens on the
   port it declines to start a second panel (`:447`–`:451`).

`stop` uses `SIGTERM` for direct processes with a 20 s wait, then `SIGKILL`
(`stop_one`, `start.sh:476`–`:506`), or `systemctl --user stop` when the unit is
active (`stop_daemon`, `:508`–`:533`).

### `deploy/Dockerfile` — text/tool half only

The image is explicit that it has no audio and no display, so voice, wake word,
TTS playback, screenshots and screen automation do not work there
(`Dockerfile:1`–`:24`). It installs the text/tool subset rather than the full
`requirements.txt` (`:90`–`:101`), makes Chromium opt-in with
`--build-arg INSTALL_BROWSER=1` (`:103`–`:108`), runs as a non-root `claire` user
(uid 10001, `:114`–`:117`), and defines:

- `PYTHONPATH=/app/src`, `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`,
  `CLAIRE_ENV_FILE=/home/claire/.config/claire/claire.env` (`:73`–`:82`);
- `HEALTHCHECK ... python -m claire.platform.service --self-check --json --only
  vault --only key_store` (`:123`–`:124`);
- `STOPSIGNAL SIGINT` and `ENTRYPOINT ["python", "-m", "claire"]`
  (`:126`–`:127`).

No secret is baked in: the image never uses `ARG *_API_KEY`, `ENV ..._KEY=...` or
`COPY *.env` (`Dockerfile:26`–`:30`), and the build context is expected to be
trimmed with a `.dockerignore` the repo does not ship (`:32`–`:52`).

## Data and control flow

### Interactive path (`./start.sh` → daemon)

1. `start.sh` resolves the venv, ensures dependencies, and runs the self-check.
2. It spawns `python -m claire` detached with `PYTHONPATH=<repo>/src` and
   `PYTHONUNBUFFERED=1`, appending stdout and stderr to
   `$LOG_DIR/daemon.log` (`_spawn`, `start.sh:349`–`:364`).
3. `python -m claire` → `__main__.py:3` → `cli.main` → `daemon.main`
   (`cli.py:49`–`:51`).
4. In `daemon.main` (`daemon.py:1386`): reset the stop flag (`:1398`), install
   signal handlers (`:1401`, handler at `:783`–`:794`), `load_settings()`
   (`:1403`), open the `Database` (`:1404`).
5. MCP preflight discovery (`:1428`–`:1458`), then `DialogueMemory`
   (`:1462`–`:1465`), warm-profile graph listener wiring (`:1468`–`:1520`),
   legacy graph migration (`:1525`–`:1533`), location (`:1535`–`:1557`).
6. TTS (`:1563`–`:1567`), the voice listener thread (`:1571`–`:1576`), then the
   phone bridge (`:1581`).
7. If `smoke_test`, print `SMOKE_TEST_INIT_OK`, stop the pieces and return
   (`:1583`–`:1622`).
8. Otherwise start the voice-feedback worker (`:1624`) and, when the platform or
   `CLAIRE_STDIN_IPC=1` calls for it, the stdin monitor (`:1630`–`:1666`,
   started at `:1674`–`:1683`), then enter the main loop (`:1685`–`:1703`): a 1 s
   tick that runs the periodic diary update under the single-writer chat lock.
   After the loop the voice thread is kept alive until it exits
   (`:1704`–`:1712`).
9. On `KeyboardInterrupt` or stop request, the `finally` block
   (`daemon.py:1716`) stops the feedback worker (`:1717`), the voice thread and
   TTS, calls `shutdown_runtime()` for MCP (`:1763`–`:1764`), closes the
   database, and runs a final diary update (`:1754`).

### systemd path

`systemctl --user start claire` runs `ExecStart=` (`claire.service:77`) inside
the user session, with `EnvironmentFile=` loaded if present (`:76`). The daemon
then follows the same boot order as above. Journald captures stdout/stderr.
`systemctl --user stop claire` sends `KillSignal=SIGINT` (`:80`) and waits
`TimeoutStopSec=20` (`:81`).

### Shutdown signal handling

`_install_signal_handlers` maps `SIGINT`, `SIGTERM` and `SIGBREAK` to the same
`KeyboardInterrupt`-raising handler (`daemon.py:783`–`:794`), which is why the
unit's `SIGINT` and `start.sh`'s `SIGTERM` both reach the same cleanup path.

## Configuration and environment

### Files

| What | Path | Override |
|---|---|---|
| Config | `~/.config/claire/config.json` (0600 on save) | `CLAIRE_CONFIG_PATH`, `XDG_CONFIG_HOME` (`config.py:830`–`:841`) |
| Runtime env file (0600) | `~/.config/claire/claire.env` | `CLAIRE_ENV_FILE` (`config.py:794`–`:809`, `service.py:404`) |
| Memory database | `~/.local/share/claire/claire.db` | `db_path` default (`config.py:547`, `_default_db_path` at `:67`) |

`load_settings()` merges `config.json` over `get_default_config()`
(`config.py:830`, `:851`–`:852`). Config keys that change deployment behaviour
include `db_path` (default from `config.py:547`), `voice_debug` (`:856`, env
`CLAIRE_VOICE_DEBUG`), `llm_base_url` (defaults to
`http://127.0.0.1:20129/v1`, `config.py:47`/`:896`), `llm_chat_model`
(`config.py:909`), `tts_engine` (default `"fish"`, `:617`), `stt_engine`
(default `"parakeet"`, `:676`), `wake_word` (default `"claire"`, `:651`) and
`mcps` (`:781`).

### Environment variables the code reads

- `CLAIRE_CONFIG_PATH` — config file location (`config.py:835`).
- `CLAIRE_ENV_FILE` — runtime env file location (`config.py:794`–`:809`).
- `CLAIRE_VOICE_DEBUG` — enables `debug_log` (`config.py:856`).
- `CLAIRE_PHONE_BRIDGE` — `0`/`false`/`no` disables the phone bridge
  (`daemon.py:1295`).
- `OMNIROUTE_API_KEY` — the LLM key, read from the environment; never stored in
  `config.json` (`config.py:47`–`:48`, `:901`).
- `CLAIRE_KEY_STORE`, `CLAIRE_VAULT_DIR`, `CLAIRE_BACKUP_ROOT`,
  `CLAIRE_SCHEDULE_PATH`, `CLAIRE_WHISPER_BACKEND` — feature-specific path and
  backend overrides read elsewhere in `config.py`.
- `CLAIRE_STDIN_IPC` — tells the daemon it owns stdin and should run the stdin
  monitor (`daemon.py:1674`–`:1680`).

### Environment variables `start.sh` honours

`CLAIRE_VENV`, `CLAIRE_PYTHON`, `CLAIRE_STATE_DIR`, `CLAIRE_LOG_DIR`,
`CLAIRE_SERVICE_NAME`, `CLAIRE_DAEMON_MODE` (`auto|systemd|direct`),
`CLAIRE_PANEL_HOST`, `CLAIRE_PANEL_PORT`, `CLAIRE_PREFLIGHT_TIMEOUT`,
`CLAIRE_START_WAIT`, `CLAIRE_FORCE_INSTALL`, `CLAIRE_USER_UNIT_DIR`, and
`XDG_STATE_HOME`/`XDG_CONFIG_HOME` (`start.sh:30`–`:41`, `:67`–`:83`, validated
at `:897`–`:900`).

### Environment the unit and image set

`PYTHONPATH`, `PYTHONUNBUFFERED` and `NUMBA_CACHE_DIR` in the unit
(`claire.service:68`, `:70`, `:124`); `PYTHONPATH`, `PYTHONUNBUFFERED`,
`PYTHONDONTWRITEBYTECODE`, `PIP_NO_CACHE_DIR`, `PIP_DISABLE_PIP_VERSION_CHECK`
and `CLAIRE_ENV_FILE` in the image (`Dockerfile:73`–`:82`).

## Where logs go

- **Under systemd:** journald. `StandardOutput=journal`,
  `StandardError=journal`, `SyslogIdentifier=claire` (`claire.service:87`–`:89`);
  read with `journalctl --user -u claire -f` (unit comment, `:14`).
- **Direct mode (`start.sh`):** `$LOG_DIR/daemon.log` and `$LOG_DIR/panel.log`,
  default `~/.local/state/claire/logs` (`start.sh:73`–`:78`, `:418`, `:463`,
  `:472`).
- **In-process debug:** `debug_log` prints to stderr and is a no-op unless
  `voice_debug` is on (`debug.py:25`–`:36`, `config.py:856`). There is no log
  file written by the daemon itself and no rotation to configure.

## Extension points

- **Change the daemon command or its environment.** Edit `ExecStart=` /
  `Environment=` in `deploy/claire.service` and re-run
  `./start.sh --install-service`; it backs up the installed unit and reloads
  systemd (`start.sh:810`–`:822`).
- **Add a new supervised component.** Add a `command_*` function plus a case arm
  in `start.sh` (`:902`–`:910`), spawn it with `_spawn` (`:349`) into
  `$STATE_DIR/run/*.pid` and `$LOG_DIR/*.log`, and teach `command_status`
  (`:649`) about it. Starting a component from inside the daemon is the existing
  alternative — that is how the phone bridge is wired (`daemon.py:1581`).
- **Add a config key.** Add the default in `get_default_config()` and a field on
  `Settings` in `config.py`, then read it where needed; `load_settings()` merges
  it automatically.
- **Add a health check.** Probes live in `src/claire/platform/service.py`
  (registry and `SelfCheck` around `:1021`) and are surfaced through
  `--self-check` (`:1217`). The unit's `Documentation=` points at the spec that
  governs them.
- **Container variant.** `deploy/Dockerfile` is intentionally a subset; adding
  voice would require an audio server and `/dev/snd` and contradicts the image's
  stated scope (`Dockerfile:1`–`:24`).

## Failure modes

| Situation | What the code actually does |
|---|---|
| No virtualenv / wrong Python | `ensure_venv` creates `.venv` with a 3.11+ interpreter, or fails with the exact `rm -rf` / `apt install` fix and exits `3` (`start.sh:210`–`:242`, exit codes at `:85`–`:88`). |
| `requirements.txt` missing | `hard_checks` reports it as a preflight problem and `start` stops before spawning anything (`start.sh:570`, `:684`–`:689`). |
| A live Claire runs from the venv | `ensure_deps` refuses to modify that venv unless `--force-install` / `CLAIRE_FORCE_INSTALL=1` (`start.sh:244`–`:250`, `:274`). |
| Preflight problem (config dir not writable, no Python) | `start` prints the items and returns exit code `3` without starting (`start.sh:684`–`:689`). |
| Self-check not green | Claire still starts unless `--strict` was given (`start.sh:698`–`:706`); each line names its own fix. |
| TTS unavailable at boot | `_initialize_tts_engine` returns `None` on an unknown engine, a missing voice model or a missing key; the daemon substitutes `_UnavailableTTS` and keeps running (`daemon.py:1559`–`:1567`, `_UnavailableTTS` at `:52`, `_initialize_tts_engine` at `:87`). |
| Voice recogniser fails to load | The listener runs in its own thread; the daemon still boots and prints the failure thread's guidance (`daemon.py:1571`–`:1576`; the PortAudio error text is at `listener.py:163`–`:167`). |
| MCP server fails to start | Discovery catches per server, prints the error beside the server name, and continues (`daemon.py:1444`–`:1456`); the whole preflight is wrapped again so nothing aborts boot. |
| Port in use (panel) | `create_server` binds without catching `OSError` (`src/claire/panel/server.py:4076`–`:4086`), so a taken port makes the panel exit non-zero with a traceback in `panel.log`. `start.sh` normally prevents this: it checks the port first and declines to start a second panel (`:447`–`:451`), and if the spawn still fails it waits `CLAIRE_START_WAIT` (default 60 s), tails the log and returns exit code `4` (`:466`–`:473`). |
| Daemon exits immediately (direct mode) | `start_daemon` tails the last 25 lines of `daemon.log` and returns exit code `4` (`start.sh:426`–`:430`). |
| Daemon never becomes active under systemd | After `CLAIRE_START_WAIT` (60 s) it prints `systemctl --user status` output and fails (`start.sh:404`–`:409`). |
| Unit copied without rendering | `ExecStart=` contains the literal `@CLAIRE_REPO_ROOT@`, so systemd fails to exec it; the unit says so explicitly (`claire.service:10`–`:12`) and `--install-service` refuses to install an unrendered template (`start.sh:780`–`:784`). |
| Crash loop | `Restart=on-failure` with `RestartSec=5s` and `StartLimitIntervalSec=300` / `StartLimitBurst=5` rate-limits it and reports the exhausted burst in the journal (`claire.service:56`–`:57`, `:85`–`:86`). |
| Missing runtime env file | `EnvironmentFile=` has a leading `-`, so the unit still starts (`claire.service:76`); the self-check and the provider call name the missing key afterwards (`claire.service:71`–`:75`). |

## Where its tests live

- [`tests/test_platform_service.py`](../../tests/test_platform_service.py) —
  parses `deploy/claire.service` as a user unit and asserts the sections, that
  `ExecStart=` runs `-m claire` with the daemon restart/backoff contract
  (`:861` onward, unit fixtures at `:45`–`:47`, `:115`), that
  `EnvironmentFile=` is used instead of inline secrets (`:900`), and checks the
  Dockerfile.
- [`tests/test_start_script.py`](../../tests/test_start_script.py) — the
  one-command path must stay machine-independent: `start.sh` exists and is
  executable (`:45`), parses under `bash -n` (`:50`), neither file contains a
  hardcoded home directory (`:60`), the panel port lives in the panel source
  (`:70`), the phone bridge is not started twice (`:78`), the documented
  interface exists (`:87`), the unit carries `@CLAIRE_REPO_ROOT@` and renders for
  any checkout (`:91`, `:99`), and the README points at `./start.sh` (`:111`).
- [`tests/test_daemon_tts_wiring.py`](../../tests/test_daemon_tts_wiring.py),
  [`tests/test_daemon_tts_engine_guard.py`](../../tests/test_daemon_tts_engine_guard.py)
  and [`tests/test_tts_startup_status.py`](../../tests/test_tts_startup_status.py)
  — boot-time TTS wiring and the unavailable-engine path.
- [`tests/test_runtime_env_file.py`](../../tests/test_runtime_env_file.py) and
  [`tests/test_version_stamp.py`](../../tests/test_version_stamp.py) — the 0600
  env file and the `python -m claire` version path.

## Documentation drift

1. **The unit still calls Whisper a "documented degraded fallback".** The
   `ReadWritePaths=` comment says `%h/.cache` is granted "for the speech
   recognition fallback" and that "Whisper - the documented degraded fallback
   for when that engine cannot load" downloads there
   (`claire.service:100`–`:106`), and the `NUMBA_CACHE_DIR=` comment says "The
   authoritative recogniser silently fell back to Whisper and Claire ran
   degraded, with nothing in the banner to say so"
   (`claire.service:121`–`:123`). The code says the opposite: Whisper is opt-in
   only and "is never a fallback" for Parakeet
   (`src/claire/listening/listener.py:170`–`:176`, `_configures_whisper` at
   `:2053`–`:2055`), and `daemon.main` prints the recogniser banner with no
   Whisper fallback advertised because "advertising a Whisper fallback would be
   a lie printed at every boot" (`src/claire/daemon.py:1414`–`:1426`). The
   hardening (a writable numba cache) is still correct; only the
   fallback narrative is stale.
2. **Not a contradiction, but worth knowing:** the unit's `KillSignal=` comment
   says "SIGINT, not SIGTERM" (`claire.service:78`–`:79`), while the daemon
   installs the same clean-shutdown handler for `SIGINT`, `SIGTERM` and
   `SIGBREAK` (`daemon.py:789`–`:790`); `start.sh` stops direct-mode processes
   with `SIGTERM` (`start.sh:489`). Both signals reach the same cleanup path.
3. **The service spec does not record the template or its installer.**
   `src/claire/platform/service.spec.md` documents the unit's facts
   (`Restart=`/`RestartSec=` at `:151`–`:153`, the key-file rule at `:154`–`:164`)
   but never says the file is a template or that `./start.sh --install-service`
   renders it. Nothing in the spec is wrong; the install step is simply absent
   from it.
4. **No drift in the README's deployment section, and none in the unit's
   template note.** `README.md` describes the template and points at
   `./start.sh --install-service` (`README.md:706`–`:726`), matching
   `claire.service:3`–`:12` and `start.sh:769`–`:829`. The unit's
   `WorkingDirectory=` rationale now explains the placeholder correctly
   (`claire.service:61`–`:63`) and the "KEYS ARE NOT IN THIS FILE" block
   (`:37`–`:43`) agrees with `AGENTS.md:5` and `:8`; both were stale earlier in
   this session and have since been corrected by another agent.

## Manual or missing

- **Rendering the unit is mandatory and is scripted, not automatic.** Nothing
  installs the unit on your behalf; `./start.sh --install-service` must be run
  once per checkout, and `systemctl --user enable --now claire` separately.
- **The unit is Linux-only and session-bound.** Claire runs while the user is
  logged in; `loginctl enable-linger` keeps the process alive without a login but
  the audio and display parts are unavailable in that mode (`claire.service:33`–`
  :36`).
- **The panel and the desktop app have no unit.** They are started by `start.sh`
  (`:432`–`:474`) or `scripts/run_desktop_app.sh` respectively, and are not
  supervised or restarted.
- **The Dockerfile ships no `.dockerignore`.** The file tells you to write one
  and lists what to exclude; it is not in the repository (`Dockerfile:32`–`:52`).
- **Container voice is out of scope by design**, not stubbed: the image installs
  `libportaudio2` only so the self-check can report the truth
  (`Dockerfile:58`–`:71`).
- **`scripts/build_installer.sh` produces a bundle directory, not an installer**
  ("There is no Inno-equivalent installer step, so the bundle directory itself is
  the artefact you'd ship", `scripts/build_installer.sh:1`–`:4`), and it rewrites
  `src/claire/_version.py` in the working tree.
