# Deployment and startup

**Authoritative spec:** [`src/claire/platform/service.spec.md`](../../src/claire/platform/service.spec.md)
(the health/self-check contract and the facts the systemd unit must have).
Read this when you are changing how Claire is started or supervised — the
systemd user unit, the one-command `start.sh`, the console entry points, the
daemon's startup order, the environment it reads, or where its output goes.

The shipped artifacts are themselves authoritative and are quoted below:
[`deploy/claire.service`](../../deploy/claire.service),
[`deploy/claire-panel.service`](../../deploy/claire-panel.service),
[`deploy/claire-desktop.service`](../../deploy/claire-desktop.service),
[`deploy/claire-desktop.desktop`](../../deploy/claire-desktop.desktop) and
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

Plainly: **each of the three parts has its own unit.** `deploy/claire.service`
starts the daemon, `deploy/claire-panel.service` starts the web panel
(`python -m claire.panel.server`), and `deploy/claire-desktop.service` starts the
tray app, which an XDG autostart entry also triggers on desktops that never
activate `graphical-session.target`. `./start.sh --install-service` renders and
enables all four artifacts; the exact directives are specified in
[`service.spec.md`](../../src/claire/platform/service.spec.md). The phone bridge
is started by the daemon itself (`daemon.py:1624`), which is why `start.sh` must
not start it too.

## Entry points

| Command | Lands at | Notes |
|---|---|---|
| `claire` (console script) | `pyproject.toml:29` → `claire.cli:main` | Installed by `pip install -e .` or a wheel; in a bare checkout the unit does not use it. |
| `python -m claire` | `src/claire/__main__.py:3` → `claire.cli:main` | What the unit's `ExecStart=` runs (unit:87) and what `start.sh` spawns (`-m claire`). |
| `python -m claire.main` | `src/claire/main.py:9` → `claire.daemon:main` | Legacy/desktop path; the desktop app spawns it (`src/desktop_app/app.py`, the `["-m", "claire.main"]` argv). |
| `python -m claire.daemon` | `src/claire/daemon.py:1831` | Reads `--smoke-test` straight from `sys.argv`. |
| `python -m claire.panel.server` | `src/claire/panel/server.py:4151` (`main`) | The web panel; its own unit, `deploy/claire-panel.service`. |
| `python -m claire.platform.service --self-check` | `src/claire/platform/service.py` (`main`; `--self-check` is one of its flags) | Health report used by `start.sh check`, the unit's `Documentation=` target, and the Dockerfile `HEALTHCHECK`. |

`claire.cli` accepts exactly three flags — `--smoke-test` (`cli.py:19`–`:23`),
`--version` (`cli.py:24`–`:28`) and `--no-tune` (`cli.py:29`–`:37`) — then hands
off to `daemon.main(smoke_test=..., no_tune=...)` (`cli.py:58`–`:60`).
`--smoke-test` initialises every subsystem, prints `SMOKE_TEST_INIT_OK`, tears
the subsystems down and returns without entering the loop
(`daemon.py:1626`–`:1665`).

`--no-tune` is the one flag that changes a setting, and only for the run: it
silences the thinking tune — the pad that pulses while Claire thinks, which is
the only cue sound she makes. The daemon applies it with `dataclasses.replace`
on a frozen `Settings` (`daemon.py` `_apply_cli_overrides`) rather than mutating
a config another thread may already hold, and `CLAIRE_TUNE_ENABLED=0` is the same
switch for the installed service, where nobody types flags — put it in
`claire.env` and restart. See
[configuration.md](configuration.md#precedence) for the environment layer and
the README for the flag list.

## Key artifacts and code anchors

### `deploy/claire.service` — a systemd **user** unit, shipped as a template

The file is a template: it contains `@CLAIRE_REPO_ROOT@` where the checkout path
belongs and refuses to be used unrendered (`claire.service:3`–`:12`). The
renderer is `start.sh --install-service` (`start.sh:1101`–`:1171`), whose
`render_unit` helper substitutes the placeholder in pure bash so a `&`, `/` or
`\` in the checkout path stays literal (`start.sh:1052`–`:1062`), then reloads
systemd (`start.sh:1138`). The unit file spells out why it is a user unit
and not a system unit (`claire.service:18`–`:34`).

Exact directives (current template, paired with their symbol):

| Directive | Value | Line |
|---|---|---|
| `[Unit] Description=` | `Claire - always-on Linux voice assistant` | `:56` |
| `Documentation=` | `file://@CLAIRE_REPO_ROOT@/src/claire/platform/service.spec.md` | `:57` |
| `After=` | `graphical-session.target pipewire.service wireplumber.service network-online.target` | `:60` |
| `Wants=` / `PartOf=` | `network-online.target` / `graphical-session.target` | `:61`–`:62` |
| `StartLimitIntervalSec=` / `StartLimitBurst=` | `300` / `5` | `:66`–`:67` |
| `[Service] Type=` | `simple` | `:70` |
| `WorkingDirectory=` | `@CLAIRE_REPO_ROOT@` | `:74` |
| `Environment=PYTHONPATH=` | `@CLAIRE_REPO_ROOT@/src` | `:78` |
| `Environment=PYTHONUNBUFFERED=` | `1` | `:80` |
| `EnvironmentFile=` | `-%h/.config/claire/claire.env` (leading `-` = optional) | `:86` |
| `ExecStart=` | `@CLAIRE_REPO_ROOT@/.venv/bin/python -m claire` | `:87` |
| `KillSignal=` | `SIGINT` | `:90` |
| `TimeoutStopSec=` | `20` | `:91` |
| `Restart=` / `RestartSec=` | `on-failure` / `5s` | `:95`–`:96` |
| `StandardOutput=` / `StandardError=` / `SyslogIdentifier=` | `journal` / `journal` / `claire` | `:97`–`:99` |
| Hardening | `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=strict`, `ProtectHome=read-only` | `:103`–`:106` |
| `ReadWritePaths=` | `%h/.local/share/claire %h/.config/claire %h/.cache/claire %h/.cache/huggingface %h/.cache/numba` | `:119` |
| `Environment=NUMBA_CACHE_DIR=` | `%h/.cache/numba` | `:134` |
| `[Install] WantedBy=` | `default.target` | `:139` |

Keys are **not** in this file: `EnvironmentFile=` points at a 0600 env file and
there is no inline `Environment=...KEY=...`. The file says so twice, in its own
"KEYS ARE NOT IN THIS FILE" block (`claire.service:38`–`:43`) and next to the
directive (`:81`–`:86`).

### Machine-specific policy goes in a drop-in, never in the unit

`./start.sh --install-service` re-renders `claire.service` from
`deploy/claire.service`, so anything hand-edited into the installed unit is lost
the next time the installer runs. That has already cost one setting on this
project: an `After=`/`Wants=omniroute.service` ordering was hand-added to the
unit and silently disappeared when the unit was re-rendered.

Local policy therefore belongs in `~/.config/systemd/user/claire.service.d/*.conf`,
which systemd merges over the unit and which the installer never writes to. A
drop-in that waits for the provider Claire warms up at startup:

```ini
# ~/.config/systemd/user/claire.service.d/omniroute-ordering.conf
[Unit]
After=omniroute.service
Wants=omniroute.service
```

```bash
systemctl --user daemon-reload
systemctl --user show claire -p After -p Wants   # confirm the merge
```

Both directives are weak: `Wants=` starts the provider if it exists and is
enabled, and neither fails the unit when it does not, so a machine with no
`omniroute.service` is unaffected. `--install-service` is tested to leave this
directory alone.

### `start.sh` — the single startup script

`start.sh` is the one-command entry point ("Claire - one command that brings the
whole system up, and takes it down again", `start.sh:3`). It locates the repo
from its own path (`start.sh:64`–`:73`), derives every default (`:78`–`:111`), and
dispatches one of `start`, `stop`, `restart`, `status`, `check` or
`install-service` (`:1260`–`:1268`).

What it does on `start` (`command_start`, `start.sh:970`–`:1028`), in order:

1. Resolve the interpreter into the parent shell (`ensure_pybin`, `:236`–`:238`),
   over `resolve_pybin` (`:221`–`:229`) and `find_python` (`:199`–`:216`). The
   parent-shell step matters: a command substitution runs in a subshell, so a
   resolution inside one never reaches the port probe that needs it.
2. Preflight checks (`hard_checks`, `:786`–`:852`), including "is this a Claire
   checkout", venv presence, `requirements.txt`, writability of the config dir,
   and whether the runtime env file exists.
3. Create `.venv` if missing (`ensure_venv`, `:244`–`:276`) and install
   `requirements.txt` when its hash changed, without touching a venv a live
   Claire runs from (`ensure_deps`, `:286`–`:321`, hash at `:240`–`:242`).
4. Run Claire's own self-check (`self_check`, `:854`–`:868`), and stop on failure
   only with `--strict`.
5. Start the daemon (`start_daemon`, `:463`–`:527`): through `systemctl --user
   start claire` when the unit is installed, otherwise as a detached process
   whose pid file is trusted only after `/proc` confirms the command line
   (`_spawn`, `:446`–`:461`; pid check, `:131`–`:150`).
6. Start the web panel (`start_panel`, `:529`–`:603`) on the panel's own
   `DEFAULT_BIND_HOST` / `DEFAULT_PORT` read out of `panel/server.py`
   (`panel_default`, `:422`–`:428`; defaults are `127.0.0.1` and `8765`,
   `src/claire/panel/server.py:151`–`:152`). If something already listens on the
   port it declines to start a second panel.
7. Start the tray app (`start_desktop`, `start.sh:608`–`:674`) through
   `claire-desktop.service` when the unit is installed, otherwise as a detached
   process. It is the one step that needs a session to draw in, so it is skipped
   rather than retried when there is none.

`stop` covers all three components: `SIGTERM` for direct processes with a 20 s
wait, then `SIGKILL` (`stop_one`, `start.sh:676`–`:706`), or `systemctl --user
stop` when the unit is active (`stop_daemon`, `:708`–`:733`). `stop_component`
(`:739`–`:761`) is the shared form behind `stop_daemon`, `stop_panel` and
`stop_desktop`.

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

### What the production install carries — the development trees are excluded

The suites stay in the repository and stay runnable from a checkout; what is
excluded is their presence in a *deployed* or *built* install. That is enforced
in three separate places, and the distinction matters because each one covers a
different file set.

**The deployed install is the checkout, not site-packages.** The venv holds an
*editable* install — its only trace in site-packages is
`__editable__.claire-<version>.pth`, whose single line is the absolute path of
the checkout's `src/` — and the unit does not depend on that either: it sets
`Environment=PYTHONPATH=<checkout>/src` and runs
`ExecStart=<checkout>/.venv/bin/python -m claire` (`claire.service:78`, `:87`).
`tests/` and `evals/` therefore sit *beside* the running code rather than inside
it, and nothing under `src/claire` imports either one.

**A wheel ships only the runtime packages.** `pyproject.toml:51`–`:54` names the
runtime roots positively (`include = ["claire*", "desktop_app*"]`) and the
development trees negatively (`exclude`, ten patterns). The negative list is the
load-bearing half: `include` alone would still admit a `tests` package created
under `src/` by a stray `__init__.py`, or by a rename that broadens the pattern.
`package-dir = { "" = "src" }` (`:34`–`:35`) is why discovery cannot see the
top-level `tests/`, `evals/` and `scripts/` directories at all; `exclude` is what
keeps that true if one of them is ever moved under `src/`.

**An sdist ships only the runtime too, and not because of any of the above.**
Distutils' sdist command appends `tests/test*.py` and `test/test*.py` to *every*
source distribution from a default in `_add_defaults_optional()`
(`setuptools/_distutils/command/sdist.py`), which `packages.find` has no say
over. `MANIFEST.in:25`–`:32` `prune`s the eight development trees; `prune` is
processed after that default, so it is what removes the files. This rule fixes a
real leak rather than stating an existing one: before `MANIFEST.in` existed, an
sdist of this repository carried **201 files under `tests/`**, and adding
`exclude` to `pyproject.toml` changed that number by **zero**.

The container follows the same boundary for the same reason: the image copies
`requirements.txt` and `src/` only (`Dockerfile:90`, `:110`), so there is no
`tests/` in it to exclude.

**To verify the artifacts**, from the repository root. This build is offline —
it uses the venv's own setuptools with no build isolation and no network — and it
writes only gitignored paths into the checkout:

```
.venv/bin/python - <<'PY'
import os, sys, tarfile, tempfile, zipfile
import setuptools.build_meta as build
out = tempfile.mkdtemp()
wheel, sdist = build.build_wheel(out), build.build_sdist(out)
DEV = ('tests/', 'evals/', 'scripts/')
print('wheel dev members:',
      [n for n in zipfile.ZipFile(os.path.join(out, wheel)).namelist()
       if n.startswith(DEV)])
tar = tarfile.open(os.path.join(out, sdist))
strip = len(tar.getnames()[0].split('/')[0]) + 1
print('sdist dev members:',
      [n[strip:] for n in tar.getnames() if n[strip:].startswith(DEV)])
PY
```

Both lists must print empty, while the wheel still holds the `claire/` package
tree, `desktop_app/` and the console entry point. Measured on this repository:
the wheel was already clean before the change (139 members, 0 development
members) and is unchanged after it (139 members); the sdist went from 369
members with 201 development files to 168 members with none.

`tests/test_prod_packaging_excludes_dev_trees.py` pins the same guarantee in the
gate without building anything — it parses both configuration files, re-runs
`find_packages` over `src/`, checks that a decoy `tests` package under a
synthetic `src/` is still rejected when `include` is relaxed to `*`, and replays
the sdist file list (legacy default first, then `MANIFEST.in`) to assert the
`prune` rules remove every `tests/test*.py` and no runtime file.

## Data and control flow

### Interactive path (`./start.sh` → daemon)

1. `start.sh` resolves the venv, ensures dependencies, and runs the self-check.
2. It spawns `python -m claire` detached with `PYTHONPATH=<repo>/src` and
   `PYTHONUNBUFFERED=1`, appending stdout and stderr to
   `$LOG_DIR/daemon.log` (`_spawn`, `start.sh:446`–`:461`).
3. `python -m claire` → `__main__.py:3` → `cli.main` → `daemon.main`
   (`cli.py:58`–`:60`).
4. In `daemon.main` (`daemon.py:1426`): reset the stop flag (`:1440`), install
   signal handlers (`:1443`, handler at `:809`–`:810`), `load_settings()`
   (`:1445`), open the `Database` (`:1446`).
5. MCP preflight discovery (`:1470`–`:1500`), then `DialogueMemory`
   (`:1504`–`:1508`), warm-profile graph listener wiring (`:1510`–`:1562`),
   legacy graph migration (`:1564`–`:1575`), location (`:1577`–`:1589`).
6. TTS (`:1591`–`:1610`), the voice listener thread (`:1612`–`:1619`), then the
   phone bridge (`:1621`–`:1624`).
7. If `smoke_test`, print `SMOKE_TEST_INIT_OK`, stop the pieces and return
   (`:1626`–`:1665`).
8. Otherwise start the voice-feedback worker (`:1667`) and, when the platform or
   `CLAIRE_STDIN_IPC=1` calls for it, the stdin monitor (`:1682`–`:1709`,
   started at `:1724`–`:1726`), then enter the main loop (`:1728`–`:1745`): a 1 s
   tick that runs the periodic diary update under the single-writer chat lock.
   After the loop the voice thread is kept alive until it exits
   (`:1747`–`:1755`).
9. On `KeyboardInterrupt` or stop request, the `finally` block
   (`daemon.py:1759`) stops the feedback worker (`:1760`), the voice thread
   (`:1764`–`:1771`), runs a final diary update (`:1796`), stops TTS (`:1801`),
   calls `shutdown_runtime()` for MCP (`:1805`–`:1807`) and closes the
   database (`:1811`).

### systemd path

`systemctl --user start claire` runs `ExecStart=` (`claire.service:87`) inside
the user session, with `EnvironmentFile=` loaded if present (`:86`). The daemon
then follows the same boot order as above. Journald captures stdout/stderr.
`systemctl --user stop claire` sends `KillSignal=SIGINT` (`:90`) and waits
`TimeoutStopSec=20` (`:91`).

### Shutdown signal handling

`_install_signal_handlers` maps `SIGINT`, `SIGTERM` and `SIGBREAK` to the same
`KeyboardInterrupt`-raising handler (`daemon.py:807`–`:818`), which is why the
unit's `SIGINT` and `start.sh`'s `SIGTERM` both reach the same cleanup path.

## Configuration and environment

### Files

| What | Path | Override |
|---|---|---|
| Config | `~/.config/claire/config.json` (0600 on save) | `CLAIRE_CONFIG_PATH` (`config.py:868`–`:869`), `XDG_CONFIG_HOME` (`default_config_path()`, `config.py:347`–`:351`) |
| Runtime env file (0600) | `~/.config/claire/claire.env` | `CLAIRE_ENV_FILE` (`config.py:810`, `runtime_env_file()` at `:801`–`:816`; `default_env_file()` in service.py) |
| Memory database | `~/.local/share/claire/claire.db` | `db_path` default (`config.py:561`, `_default_db_path` at `:83`) |

`load_settings()` merges `config.json` over `get_default_config()`
(`config.py:883`–`:885`). Config keys that change deployment behaviour
include `db_path` (default from `config.py:561`), `voice_debug` (`:889`, env
`CLAIRE_VOICE_DEBUG`), `llm_base_url` (defaults to
`http://127.0.0.1:20129/v1`, `config.py:47`/`:922`–`:930`), `llm_chat_model`
(`config.py:934`–`:947`), `tts_engine` (default `"fish"`, `:630`), `stt_engine`
(default `"parakeet"`, `:688`), `wake_word` (default `"claire"`, `:664`) and
`mcps` (`:788`).

### Environment variables the code reads

- `CLAIRE_CONFIG_PATH` — config file location (`config.py:868`).
- `CLAIRE_ENV_FILE` — runtime env file location (`config.py:801`–`:816`, read at `:810`).
- `CLAIRE_VOICE_DEBUG` — enables `debug_log` (`config.py:889`).
- `CLAIRE_PHONE_BRIDGE` — `0`/`false`/`no` disables the phone bridge
  (`daemon.py:1319`).
- `OMNIROUTE_API_KEY` — the LLM key, read from the environment; never stored in
  `config.json` (`config.py:47`–`:48`, `:933`).
- `CLAIRE_KEY_STORE`, `CLAIRE_VAULT_DIR`, `CLAIRE_BACKUP_ROOT`,
  `CLAIRE_SCHEDULE_PATH`, `CLAIRE_WHISPER_BACKEND` — feature-specific path and
  backend overrides read elsewhere in `config.py`.
- `CLAIRE_STDIN_IPC` — tells the daemon it owns stdin and should run the stdin
  monitor (`daemon.py:1717`–`:1722`).

### Environment variables `start.sh` honours

`CLAIRE_VENV`, `CLAIRE_PYTHON`, `CLAIRE_STATE_DIR`, `CLAIRE_LOG_DIR`,
`CLAIRE_SERVICE_NAME`, `CLAIRE_DAEMON_MODE` (`auto|systemd|direct`),
`CLAIRE_PANEL_HOST`, `CLAIRE_PANEL_PORT`, `CLAIRE_PREFLIGHT_TIMEOUT`,
`CLAIRE_START_WAIT`, `CLAIRE_FORCE_INSTALL`, `CLAIRE_USER_UNIT_DIR`, and
`XDG_STATE_HOME`/`XDG_CONFIG_HOME` (`start.sh:35`–`:52`, `:78`–`:111`, queued
at `:1245`–`:1257`).

### Environment the unit and image set

`PYTHONPATH`, `PYTHONUNBUFFERED` and `NUMBA_CACHE_DIR` in the unit
(`claire.service:78`, `:80`, `:134`); `PYTHONPATH`, `PYTHONUNBUFFERED`,
`PYTHONDONTWRITEBYTECODE`, `PIP_NO_CACHE_DIR`, `PIP_DISABLE_PIP_VERSION_CHECK`
and `CLAIRE_ENV_FILE` in the image (`Dockerfile:73`–`:82`).

## Where logs go

- **Under systemd:** journald. `StandardOutput=journal`,
  `StandardError=journal`, `SyslogIdentifier=claire` (`claire.service:97`–`:99`);
  read with `journalctl --user -u claire -f` (unit comment, `:14`).
- **Direct mode (`start.sh`):** `$LOG_DIR/daemon.log` and `$LOG_DIR/panel.log`,
  default `~/.local/state/claire/logs` (`start.sh:89`–`:91`; written at `:515`,
  `:592`, `:664`).
- **In-process debug:** `debug_log` prints to stderr and is a no-op unless
  `voice_debug` is on (`debug.py:25`–`:36`, `config.py:889`). There is no log
  file written by the daemon itself and no rotation to configure.

## Extension points

- **Change the daemon command or its environment.** Edit `ExecStart=` /
  `Environment=` in `deploy/claire.service` and re-run
  `./start.sh --install-service`; it backs up the installed unit and reloads
  systemd (`start.sh:1066`–`:1138`).
- **Add a new supervised component.** Add a `command_*` function plus a case arm
  in `start.sh` (`:1260`–`:1268`), spawn it with `_spawn` (`:446`) into
  `$STATE_DIR/run/*.pid` and `$LOG_DIR/*.log`, and teach `command_status`
  (`:895`) about it. Starting a component from inside the daemon is the existing
  alternative — that is how the phone bridge is wired (`daemon.py:1624`).
- **Add a config key.** Add the default in `get_default_config()` and a field on
  `Settings` in `config.py`, then read it where needed; `load_settings()` merges
  it automatically.
- **Add a health check.** Probes live in `src/claire/platform/service.py`
  (the probe registry and the `SelfCheck` class) and are surfaced through
  `--self-check`. The unit's `Documentation=` points at the spec that
  governs them.
- **Container variant.** `deploy/Dockerfile` is intentionally a subset; adding
  voice would require an audio server and `/dev/snd` and contradicts the image's
  stated scope (`Dockerfile:1`–`:24`).

## Failure modes

| Situation | What the code actually does |
|---|---|
| No virtualenv / wrong Python | `ensure_venv` creates `.venv` with a 3.11+ interpreter, or fails with the exact `rm -rf` / `apt install` fix and exits `3` (`start.sh:244`–`:276`, exit codes at `:108`–`:111`). |
| `requirements.txt` missing | `hard_checks` reports it as a preflight problem and `start` stops before spawning anything (`start.sh:815`–`:816`, `:976`–`:979`). |
| A live Claire runs from the venv | `ensure_deps` refuses to modify that venv unless `--force-install` / `CLAIRE_FORCE_INSTALL=1` (`start.sh:305`–`:308`). |
| Preflight problem (config dir not writable, no Python) | `start` prints the items and returns exit code `3` without starting (`start.sh:976`–`:979`). |
| Self-check not green | Claire still starts unless `--strict` was given (`start.sh:989`–`:996`); each line names its own fix. |
| TTS unavailable at boot | `_initialize_tts_engine` returns `None` on an unknown engine, a missing voice model or a missing key; the daemon substitutes `_UnavailableTTS` and keeps running (`daemon.py:1591`–`:1610`, `_UnavailableTTS` at `:53`, `_initialize_tts_engine` at `:88`). |
| Voice recogniser fails to load | The listener runs in its own thread; the daemon still boots and prints the failure thread's guidance (`daemon.py:1612`–`:1619`; the PortAudio error text is in `src/claire/listening/listener.py`, in the `except` arm that prints "PortAudio initialisation failed"). |
| MCP server fails to start | Discovery catches per server, prints the error beside the server name, and continues (`daemon.py:1486`–`:1491`); the whole preflight is wrapped again so nothing aborts boot. |
| Port in use (panel) | `create_server` binds without catching `OSError` (`src/claire/panel/server.py:4105`–`:4114`), so a taken port makes the panel exit non-zero with a traceback in `panel.log`. `start.sh` normally prevents this: it checks the port first and declines to start a second panel (`start.sh:548`–`:552`), and if the spawn still fails it waits `CLAIRE_START_WAIT` (default 60 s), tails the log and returns exit code `4` (`start.sh:595`–`:602`). |
| Daemon exits immediately (direct mode) | `start_daemon` tails the last 25 lines of `daemon.log` and returns exit code `4` (`start.sh:525`–`:526`, mapped to `EXIT_STARTUP` at `:1002`). |
| Daemon never becomes active under systemd | After `CLAIRE_START_WAIT` (60 s) it prints `systemctl --user status` output and fails (`start.sh:505`–`:506`). |
| Unit copied without rendering | `ExecStart=` contains the literal `@CLAIRE_REPO_ROOT@`, so systemd fails to exec it; the unit says so explicitly (`claire.service:10`–`:12`) and `--install-service` refuses to install an unrendered template (`start.sh:1072`–`:1075`). |
| Crash loop | `Restart=on-failure` with `RestartSec=5s` and `StartLimitIntervalSec=300` / `StartLimitBurst=5` rate-limits it and reports the exhausted burst in the journal (`claire.service:66`–`:67`, `:95`–`:96`). |
| Missing runtime env file | `EnvironmentFile=` has a leading `-`, so the unit still starts (`claire.service:86`); the self-check and the provider call name the missing key afterwards (`claire.service:81`–`:85`). |

## Where its tests live

- [`tests/test_platform_service.py`](../../tests/test_platform_service.py) —
  parses `deploy/claire.service` as a user unit and asserts the sections, that
  `ExecStart=` runs `-m claire` with the daemon restart/backoff contract
  (`:876` onward, unit fixtures at `:45`–`:47`, `:115`), that
  `EnvironmentFile=` is used instead of inline secrets (`:900`), and checks the
  Dockerfile.
- [`tests/test_start_script.py`](../../tests/test_start_script.py) — the
  one-command path must stay machine-independent: `start.sh` exists and is
  executable (`:51`), parses under `bash -n` (`:56`), neither file contains a
  hardcoded home directory (`:71`), the panel port lives in the panel source
  (`:80`), the phone bridge is not started twice (`:88`), the documented
  interface exists (`:97`), the unit carries `@CLAIRE_REPO_ROOT@` and renders for
  any checkout (`:101`, `:109`), and the README points at `./start.sh` (`:121`).
- [`tests/test_daemon_tts_wiring.py`](../../tests/test_daemon_tts_wiring.py),
  [`tests/test_daemon_tts_engine_guard.py`](../../tests/test_daemon_tts_engine_guard.py)
  and [`tests/test_tts_startup_status.py`](../../tests/test_tts_startup_status.py)
  — boot-time TTS wiring and the unavailable-engine path.
- [`tests/test_runtime_env_file.py`](../../tests/test_runtime_env_file.py) and
  [`tests/test_version_stamp.py`](../../tests/test_version_stamp.py) — the 0600
  env file and the `python -m claire` version path.
- [`tests/test_prod_packaging_excludes_dev_trees.py`](../../tests/test_prod_packaging_excludes_dev_trees.py)
  — the packaging boundary above. `pyproject.toml`'s `include`/`exclude` are read
  and `find_packages` is re-run over `src/`; a decoy `tests` package under a
  synthetic `src/` must still be rejected once `include` is relaxed to `*`; and
  the sdist file list is replayed (legacy default first, then `MANIFEST.in`) to
  show the `prune` rules drop every `tests/test*.py` and no runtime file.
  Deterministic and build-free.

## Documentation drift

1. **The unit still calls Whisper a "documented degraded fallback".** The
   `ReadWritePaths=` comment says `%h/.cache` is granted "for the speech
   recognition fallback" and that "Whisper - the documented degraded fallback
   for when that engine cannot load" downloads there
   (`claire.service:110`–`:113`), and the `NUMBA_CACHE_DIR=` comment says "The
   authoritative recogniser silently fell back to Whisper and Claire ran
   degraded, with nothing in the banner to say so"
   (`claire.service:131`–`:132`). The code says the opposite: Whisper is opt-in
   only and "is never a fallback" for Parakeet
   (`src/claire/listening/listener.py`, the `_configures_whisper` guard and the
   "Whisper is **opt-in only**" comment above it), and `daemon.main` prints the
   recogniser banner with no Whisper fallback advertised because "advertising a
   Whisper fallback would be a lie printed at every boot"
   (`src/claire/daemon.py:1456`–`:1462`). The
   hardening (a writable numba cache) is still correct; only the
   fallback narrative is stale.
2. **Not a contradiction, but worth knowing:** the unit's `KillSignal=` comment
   says "SIGINT, not SIGTERM" (`claire.service:88`–`:89`), while the daemon
   installs the same clean-shutdown handler for `SIGINT`, `SIGTERM` and
   `SIGBREAK` (`daemon.py:812`); `start.sh` stops direct-mode processes
   with `SIGTERM` (`start.sh:689`). Both signals reach the same cleanup path.
3. **The service spec does not record the template or its installer.**
   `src/claire/platform/service.spec.md` documents the unit's facts
   (`Restart=`/`RestartSec=` at `:159`–`:161`, the key-file rule at `:162`–`:167`)
   but never says the file is a template or that `./start.sh --install-service`
   renders it. Nothing in the spec is wrong; the install step is simply absent
   from it.
4. **No drift in the README's deployment section, and none in the unit's
   template note.** `README.md` describes the template and points at
   `./start.sh --install-service` (`README.md:751`–`:762`), matching
   `claire.service:3`–`:12` and `start.sh:1101`–`:1171`. The unit's
   `WorkingDirectory=` rationale now explains the placeholder correctly
   (`claire.service:71`–`:73`) and the "KEYS ARE NOT IN THIS FILE" block
   (`:38`–`:43`) agrees with `AGENTS.md:5` and `:8`; both were stale earlier in
   this session and have since been corrected by another agent.

## Manual or missing

- **Rendering the units is explicit, not automatic.** Nothing installs them until
  you ask: `./start.sh --install-service` must be run once per checkout. It also
  enables all three units, so no separate `systemctl --user enable` is needed.
- **The unit is Linux-only and session-bound.** Claire runs while the user is
  logged in; `loginctl enable-linger` keeps the process alive without a login but
  the audio and display parts are unavailable in that mode (`claire.service:33`–`
  :36`).
- **The tray app has no honest place at boot.** Its unit is installed and enabled
  but scoped to `graphical-session.target`; on a desktop that never activates that
  target the XDG autostart entry is what starts it, and either way it only comes up
  once there is a session to draw in.
- **The Dockerfile ships no `.dockerignore`.** The file tells you to write one
  and lists what to exclude; it is not in the repository (`Dockerfile:32`–`:52`).
- **Container voice is out of scope by design**, not stubbed: the image installs
  `libportaudio2` only so the self-check can report the truth
  (`Dockerfile:58`–`:71`).
- **`scripts/build_installer.sh` produces a bundle directory, not an installer**
  ("There is no Inno-equivalent installer step, so the bundle directory itself is
  the artefact you'd ship", `scripts/build_installer.sh:1`–`:4`), and it rewrites
  `src/claire/_version.py` in the working tree.
