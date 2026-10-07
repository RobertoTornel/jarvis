# Platform / Service Specification (R-SERVICE-1)

`src/claire/platform/service.py` answers one question: **what is actually
wrong?** Claire is an always-on process, and when she "seems broken" the owner
is standing at a terminal with no stack trace in hand. This module is the thing
they run, so it is written to three rules — each one bought with a test in
`tests/test_platform_service.py`.

## Rule 1: a crashing probe is a *reported problem*, never an exception

Every check runs through `SelfCheck._run_one`, which calls the probe inside a
guard:

* `KeyboardInterrupt` / `SystemExit` are re-raised (the owner asked to stop; that
  is not a health finding),
* **any other `BaseException`** becomes a `CheckResult` with
  `status="problem"`, the exception type and message in `detail`, and the
  exception type in `data.exception`.

This is the whole point of the module. A naive implementation lets the probe's
exception propagate: the owner, whose problem is already that something is
broken, gets a traceback from the diagnostic tool and learns nothing. The
`PROBLEM` status is kept distinct from `MISSING` on purpose — a missing binary is
a fact about the box, a crashed probe is a defect in Claire, and the fixes
differ.

The guard also normalises what a probe *returns*:

| probe returned | reported |
| --- | --- |
| `CheckResult` with a valid status and its own name | as given |
| `None` | `problem` — "probe returned no result" |
| anything that is not a `CheckResult` | `problem` — names the type |
| a `CheckResult` named something other than the registered check | `problem` |
| `missing` with no `fix` | stays `missing`, fix auto-filled with an explicit "this is a probe defect" note |

The last row matters: the contract is "MISSING **with the fix**", so a probe
that omits one must not render as an unactionable line.

## Rule 2: every MISSING names its fix

`MISSING` always carries a `fix` — a command or a setting, not a homily.
Examples of what the probes emit:

| check | what it looks at | example fix |
| --- | --- | --- |
| `stt_model` | `stt_model_path` in the config | fetch `nvidia/parakeet-tdt-0.6b-v2` or point `stt_model_path` at the export, then `python scripts/check_stt_model.py` |
| `vault` | `resolve_vault_dir()` exists, is a directory, is writable | `mkdir -p <vault> && chmod 700 <vault>` |
| `key_store` | the OS keychain backend **and** the 0600 env file (`mode`, variable *names*) | `install -m 600 /dev/null <env file>` / `chmod 600 <env file>` / `sudo apt-get install -y gnome-keyring` |
| `audio_device` | `sounddevice.query_devices(kind="input")` | `sudo apt-get install -y libportaudio2`, check `pactl list short sources`, and **run under the user unit** — a system unit has no session bus and never sees the microphone |
| `tesseract` | `claire.senses.screen.diagnose()` | the diagnosis's own `fix_command` (`sudo apt-get install -y tesseract-ocr`) |
| `browser` | `claire.senses.browser.probe_chromium_binary()` | `playwright install chromium` |
| `llm_endpoint` | `GET <llm_base_url>/models` (2s budget) | start the model server that owns the endpoint; `curl -sS <url>/models` |

Where a project error already knows its fix (`OcrEngineMissingError.command`,
`SttModelMissingError.hint`, …) the guard reuses it: `_fix_from_exception` reads
`fix_command` / `command` / `install_command` / `hint` / `fix` off whatever the
probe raised. An exception that names its own fix produces a named fix even
though the probe crashed.

Probes are deliberately cheap and side-effect free. In particular
`browser` probes **only the chromium binary**: launching a browser would take
seconds, could leave the profile directory locked (chromium takes an exclusive
`SingletonLock`), and a health check must not have side effects. Likewise the
LLM probe is a `GET /models`, not a chat completion — no tokens are spent on a
diagnostic.

## Rule 3: no secret value leaves this module

The report is printed to a terminal, written to journald by the systemd unit, and
pasted into bug reports; the project has already been bitten once by a leaked
credential caught in verification. So:

* everything rendered goes through `claire.utils.redact.redact` in `_safe_text`
  (applied to `detail`, `fix` and the strings inside `data`) as a backstop;
* endpoint URLs are stripped of userinfo, query and fragment by `redact_url`, so
  `https://user:token@host/v1?api_key=…` is reported as `https://host/v1`;
* the LLM key is reported only through `secret_fingerprint`: `present
  (last4=abcd, sha256=…)` or `absent`. The `Authorization` header is built and
  never rendered;
* the key store probe reports the env file's **mode** and the **names** of the
  variables in it. `_env_file_key_names` splits on the first `=` and discards
  everything after it without returning it — values do not exist as data in this
  process;
* the world-readable systemd unit contains no inline secret (see below), and
  `tests/test_platform_service.py` asserts that a planted key value never appears
  in the rendered or JSON report.

## The report

`HealthReport` is a frozen tuple of `CheckResult`s plus `generated_at`. Each
`CheckResult` has `name`, `status` (`ready` / `missing` / `problem`), `detail`,
`fix` and a machine-readable `data` map. `HealthReport.ok` is true only when
*every* probe answered `ready` — there is no "mostly fine".

Rendered form (this is HELP output, so stdout is correct here), one check per
line and fixes indented, so it stays greppable and parseable:

```
Claire self-check - 2026-10-06T15:22:13+00:00
[READY] Memory vault: /home/mana/.local/share/claire/vault (writable), router file present (914 bytes)
[MISSING] Tesseract OCR: The 'tesseract' binary is not installed ... Fix: sudo apt-get install -y tesseract-ocr
    fix: sudo apt-get install -y tesseract-ocr
Summary: 4 ready, 3 missing, 0 problem - NOT READY
```

Callers that should not parse text use `--json` (`HealthReport.to_json()`), which
is the same data as `{generated_at, ok, summary{ready,missing,problem,total},
checks[]}`.

## Entry point

```
python -m claire.platform.service --self-check
python -m claire.platform.service --self-check --json
python -m claire.platform.service --only vault --only key_store
```

`--self-check` is accepted (and is the default) so the command reads as what it
is. `--only` takes any of the check names and is repeatable. `--timeout` bounds
the LLM probe. Exit codes: `0` everything ready, `1` something is missing or
broken (the report says what and how to fix it), `2` the command line was wrong
(argparse). Note that a `1` from here is a *report*, not a crash: `set -e`
scripts that want the text should use `|| true`.

Injection seams exist because the tests must never need a real microphone, a
real chromium, a real tesseract binary or a live endpoint: `settings` /
`settings_loader`, `probes` (override any probe by name with a fake — including
one that raises), `http_get` (`(url, headers, timeout) -> (status, body)`) and
`now`.

## Deployment

### `deploy/claire.service` — a systemd **user** unit

The unit is a *user* unit for one non-negotiable reason, recorded in the file
itself and repeated here: **a system unit cannot reach the microphone.** PID 1
starts before any login session, so there is no `DBUS_SESSION_BUS_ADDRESS`, no
`XDG_RUNTIME_DIR`, no PipeWire/PulseAudio and no display. PortAudio then finds no
device (or cannot open the one it finds) and the failure looks like a Claire bug.
Adding `User=`/`Group=` to a system unit does not help: it changes the uid, not
the session. A user unit runs inside the session where the bus, the audio server
and the display exist.

Consequences stated in the unit file: Claire runs while that user is logged in;
`loginctl enable-linger` gives up-time without a login but *still* has no session
bus, so voice and screen senses do not work in that mode either.

Other facts worth knowing about the unit:

* `Restart=on-failure` with `RestartSec=5s`, plus `StartLimitIntervalSec=300` and
  `StartLimitBurst=5` in `[Unit]`, so a crash loop is rate-limited by systemd
  and then reported in the journal instead of spinning forever.
* Keys come from `EnvironmentFile=-%h/.config/claire/claire.env` — a 0600 file
  owned by the user, **never** an inline `Environment=...KEY=...` in this
  world-readable file. The leading `-` keeps the unit startable when the keys are
  held in the OS keychain instead; a genuinely missing key is then named with its
  fix by `--self-check` and by the provider call that needs it, so it is loud,
  not silent.
* Logging goes to journald (`StandardOutput=journal`, `StandardError=journal`,
  `SyslogIdentifier=claire`) — never to a file Claire has to rotate.
* `KillSignal=SIGINT` and `TimeoutStopSec=20`: SIGINT is what Claire handles as
  "finish the turn, close the audio stream, exit", which is what keeps the
  microphone out of a wedged state.

### `deploy/Dockerfile` — text/tool half only

The image is honest about being half a Claire. A container has no session, so
there is **no audio** (no sound server, no `/dev/snd`) and **no display** (no
X11/Wayland): voice input, wake word, TTS playback, screenshots and GUI
automation do not work there. What does work is the LLM/tool half — chat, vault,
scheduling, web fetch, OCR over an image, headless chromium — which is a useful
toolbox service and a useless voice assistant.

The Dockerfile therefore: uses a slim Python 3.12 base; installs the system
packages the text/tool half needs (`tesseract-ocr`, `libportaudio2` so that the
self-check reports the real "no input device" instead of a missing shared
library, `ca-certificates`, `curl`); installs the Python subset rather than the
full voice stack (with the exact command to opt in); makes chromium opt-in via
`--build-arg INSTALL_BROWSER=1` because it is ~400MB; copies `requirements.txt`
and `src/` only; creates and switches to a non-root user `claire` (uid 10001, no
shell); bakes in **no** secret and reads keys at run time through
`CLAIRE_ENV_FILE` / `--env-file` / a secrets mount; and carries a `HEALTHCHECK`
that runs `--self-check --json --only vault --only key_store`, i.e. only the
checks that *can* be healthy in a container, rather than lying about the voice
half. The recommended `.dockerignore` is written out in the file's header.

## Tests

`tests/test_platform_service.py` covers, among others:

* **every** probe injected as a fake that raises — the report is still produced
  and that check is `problem` (the fail-proof; a naive implementation propagates
  the exception);
* a probe that returns `ready` is reported `ready`, and `HealthReport.ok` is true
  only when all of them do;
* each default probe in a genuinely missing state reports a non-empty fix;
* the systemd unit parses as a unit file: required sections, `ExecStart` runs
  `python -m claire`, restart-with-backoff directives, `EnvironmentFile`, a user
  unit (`WantedBy=default.target`), the microphone comment, and **no inline
  secret**;
* the Dockerfile exists, uses a slim Python base, has a non-root `USER`, no
  secret-shaped literal, and documents that audio and the display are absent;
* the JSON output is valid JSON and round-trips the same data as the readable
  form;
* a planted secret value never appears in the rendered or JSON report.

No test starts a service, builds an image, opens a microphone or launches a
browser: those are the acceptance criteria for the deployment files, not unit
tests. The file carries `pytestmark = pytest.mark.unit` because the gate
(`pytest tests/ -m unit`) silently deselects an unmarked file.
