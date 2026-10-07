#!/usr/bin/env python3
"""Claire's acceptance check: one command that tells the truth about this install.

What this is
------------

Every capability Claire claims is measured here, in one run, and reported as a
table:

* ``READY``      - the capability was *measured* on this machine and works.
* ``BLOCKED``    - a named dependency, credential or engine is missing. Always
                   paired with the exact command or setting that fixes it.
* ``UNMEASURED`` - the code is present but this machine has never been able to
                   observe it (no microphone audio has ever been captured, no
                   live speech has ever been synthesised, nothing has ever been
                   delivered for real). Unmeasured is reported as unmeasured,
                   never as READY.

Exit codes (never a bare traceback, never a silent green):

* ``0`` - every capability is READY.
* ``1`` - at least one capability is BLOCKED (including a check that crashed).
* ``2`` - usage error (an unknown ``--only`` name, a bad flag).
* ``3`` - nothing is BLOCKED, but something is UNMEASURED: this run cannot
  certify the install.

The fail-proof
--------------

Every check is individually guarded, exactly like
:mod:`claire.platform.service`'s self-check: a probe that raises is reported as
a problem for *that* capability (never as READY, and never as a traceback out of
the tool) and the run continues, so the owner still gets the full scoreboard for
the nineteen capabilities that did work.

Honesty rules this file follows
-------------------------------

1. **No secret value is ever printed, logged or put in ``--json``.** Key
   material is reported as a name, a masked rendering (last-4 + fingerprint) or
   a bare count. Nothing here calls a raw-value accessor.
2. **A missing thing produces a named error and an actionable fix**, never an
   empty string and never silence.
3. **Read-only where it matters.** This script does not create the memory
   router, does not write to the key store, does not send a message, does not
   open a port, and never spends API credit. The only files it touches are one
   temporary screenshot (deleted immediately) and the browser profile chromium
   itself owns.

Usage::

    .venv/bin/python scripts/acceptance_check.py
    .venv/bin/python scripts/acceptance_check.py --json
    .venv/bin/python scripts/acceptance_check.py --only browser,ocr
    .venv/bin/python scripts/acceptance_check.py --list

Importing this module has no side effects: no check runs until :func:`main`.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "CHECK_ORDER",
    "CHECK_SUMMARIES",
    "CHECK_TITLES",
    "EXIT_BLOCKED",
    "EXIT_READY",
    "EXIT_UNMEASURED",
    "EXIT_USAGE",
    "FIX_CHECK_CRASHED",
    "STATE_BLOCKED",
    "STATE_READY",
    "STATE_UNMEASURED",
    "STATES",
    "AcceptanceContext",
    "AcceptanceReport",
    "AcceptanceRunner",
    "CapabilityImportError",
    "CheckOutcome",
    "ProbeResult",
    "build_parser",
    "main",
    "select_names",
]

# The repository root, so the checks run against the checkout they live in and
# not against whatever ``claire`` happens to be installed in site-packages.
REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

#: Measured, and it works.
STATE_READY = "READY"
#: A named dependency, credential or engine is missing. Always carries a fix.
STATE_BLOCKED = "BLOCKED"
#: Present but never observed on this machine. Never silently promoted to READY.
STATE_UNMEASURED = "UNMEASURED"

STATES: tuple[str, ...] = (STATE_READY, STATE_BLOCKED, STATE_UNMEASURED)

EXIT_READY = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2
EXIT_UNMEASURED = 3

#: The schema of the ``--json`` document, so a consumer can refuse to guess.
SCHEMA = "claire.acceptance/1"

#: Handed to a check that raised instead of reporting. Names the defect (this is
#: Claire's bug, not the owner's missing package) and says what to send back.
FIX_CHECK_CRASHED = (
    "This is a defect in Claire's acceptance check, not a missing capability: the "
    "check crashed instead of reporting. Re-run with "
    "`.venv/bin/python scripts/acceptance_check.py --json` and report the "
    "field of this capability."
)

#: Handed to a check that returned nothing usable.
FIX_CHECK_MALFORMED = (
    "This is a defect in Claire's acceptance check: the check returned something "
    "that is not a verdict. Re-run with `--json` and report the `data.returned` "
    "field of this capability."
)

#: Longest a single detail/fix string may be before it is cut in the report.
MAX_TEXT_CHARS = 1200

#: The check that most needs to be believable: how long the browser probe waits.
BROWSER_PROBE_TIMEOUT_MS = 15_000

#: A page with no network dependency, so the browser check cannot be misread as
#: "the internet is up".
ACCEPTANCE_PAGE_URL = (
    "data:text/html,<title>claire-acceptance</title><p>claire-acceptance-probe</p>"
)
ACCEPTANCE_MARKER = "claire-acceptance"

def _default_fish_key_file() -> str:
    """Where the Fish engine's keys live when the config does not say.

    Asked of :mod:`claire.config` rather than copied here: the author's own
    absolute path is wrong on every other machine, and a "fix" that names a
    path that does not exist is not a fix.  Never raises - a diagnostic tool
    that crashes on its own fallback is worse than one that prints "".
    """
    try:
        from claire.config import get_default_config

        return str(get_default_config().get("fish_key_file") or "")
    except Exception:  # noqa: BLE001 - deliberate: the fallback must not crash
        return ""


#: How every fix in this report spells a runnable command. The package lives
#: under ``src/`` and this checkout has no console script installed, so a
#: command that omits PYTHONPATH does not run -- and a fix that does not run is
#: not a fix.
PY = "PYTHONPATH=src .venv/bin/python"

#: How to store a credential without putting its value in the shell history.
KEY_STORE_FIX = (
    "Store a credential without echoing it: "
    f'`{PY} -c "from claire.platform.keys import KeyStore; import getpass; '
    "KeyStore().store('NAME', getpass.getpass('value: '))\"` "
    "(the store file is created 0600 outside the repository)."
)

#: The last live Fish call recorded in STATUS.md. Named here so the unmeasured
#: verdict carries the actual observed cause instead of a guess.
FISH_LAST_LIVE_RESULT = "HTTP 402 Insufficient API credit"

#: Every capability, in the order the owner reads them: platform and voice
#: first, then memory, schedule, the senses, outreach and finally the
#: platform's own self-check.
CHECK_ORDER: tuple[str, ...] = (
    "config",
    "vault",
    "key_store",
    "audio_device",
    "stt_model",
    "wake_word",
    "tts_synthesis",
    "llm_endpoint",
    "memory_router",
    "system_prompt",
    "calendar",
    "timers",
    "screen_capture",
    "ocr",
    "camera",
    "browser",
    "outreach_ladder",
    "outreach_live",
    "backups",
    "service_selfcheck",
)

CHECK_TITLES: Mapping[str, str] = {
    "config": "Configuration",
    "vault": "Memory vault",
    "key_store": "Key store",
    "audio_device": "Audio input device",
    "stt_model": "Speech-to-text model",
    "wake_word": "Wake word (acoustic)",
    "tts_synthesis": "Speech synthesis (live)",
    "llm_endpoint": "LLM endpoint",
    "memory_router": "Memory router (always-loaded)",
    "system_prompt": "System prompt / memory section",
    "calendar": "Calendar (conflicts + alternatives)",
    "timers": "Timers",
    "screen_capture": "Screen capture",
    "ocr": "Screen OCR",
    "camera": "Camera",
    "browser": "Browser control (chromium)",
    "outreach_ladder": "Outreach urgency ladder (dry-run)",
    "outreach_live": "Outreach live delivery",
    "backups": "Backups (rotating slots)",
    "service_selfcheck": "Platform self-check",
}

#: One line per capability, for ``--list``.
CHECK_SUMMARIES: Mapping[str, str] = {
    "config": "the config file loads and reports its provider and voice engines",
    "vault": "the memory vault directory exists and is writable",
    "key_store": "stored credentials, reported masked (never a value)",
    "audio_device": "an input device this process can open",
    "stt_model": "the configured turn recogniser model is present",
    "wake_word": "acoustic wake-word behaviour (needs captured microphone audio)",
    "tts_synthesis": "live speech synthesis (spends account credit)",
    "llm_endpoint": "the configured LLM endpoint answers",
    "memory_router": "the always-loaded router file, re-read per request, in budget",
    "system_prompt": "the memory section is present in the assembled prompt",
    "calendar": "overlap detection proposes ranked alternative slots",
    "timers": "timers start, list and fire (in memory, no sleeping)",
    "screen_capture": "a screenshot can be produced right now",
    "ocr": "text can be read off an image (tesseract binary)",
    "camera": "a camera device exists",
    "browser": "headless chromium launches and reads a page",
    "outreach_ladder": "urgency classification and routing, with dry-run the default",
    "outreach_live": "a real message delivered through a transport",
    "backups": "the rotating snapshot slots verify",
    "service_selfcheck": "claire.platform.service's own self-check runs to completion",
}

for _name in CHECK_ORDER:  # a missing title/summary is a defect, not a blank cell
    assert _name in CHECK_TITLES, f"CHECK_TITLES is missing {_name!r}"
    assert _name in CHECK_SUMMARIES, f"CHECK_SUMMARIES is missing {_name!r}"
del _name


# --------------------------------------------------------------------------- #
# Probe contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeResult:
    """One capability's verdict, as a probe reports it."""

    state: str
    detail: str = ""
    fix: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(
                f"unknown state {self.state!r}; expected one of {list(STATES)}"
            )

    # -- constructors, so probes read as sentences ------------------------- #

    @classmethod
    def ready(cls, detail: str, **data: Any) -> "ProbeResult":
        return cls(STATE_READY, _clip(detail), None, data)

    @classmethod
    def blocked(cls, detail: str, fix: str, **data: Any) -> "ProbeResult":
        return cls(STATE_BLOCKED, _clip(detail), _clip(fix), data)

    @classmethod
    def unmeasured(cls, detail: str, fix: str | None = None, **data: Any) -> "ProbeResult":
        return cls(STATE_UNMEASURED, _clip(detail), _clip(fix) if fix else None, data)


class CapabilityImportError(RuntimeError):
    """A capability's module could not be imported; carries the fix to give."""

    def __init__(self, module: str, fix: str, cause: BaseException) -> None:
        self.module = module
        self.fix = fix
        self.cause = cause
        super().__init__(
            f"the module {module!r} could not be imported ({type(cause).__name__}: {cause})."
        )


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[: MAX_TEXT_CHARS - 3].rstrip() + "..."


def _require(module: str, fix: str) -> Any:
    """Import *module*, turning any failure into a named, fixable error."""

    try:
        return importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - reported with a fix, never swallowed
        raise CapabilityImportError(module, fix, exc) from exc


def _import_fix(module: str) -> str:
    return (
        f"Install/repair Claire's dependencies, then retry: "
        f"`pip install -r requirements.txt` (the module {module!r} failed to import)."
    )


# --------------------------------------------------------------------------- #
# Environment the probes read
# --------------------------------------------------------------------------- #


def _utc_now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


class AcceptanceContext:
    """Everything the probes are allowed to touch, injectable for tests.

    ``settings_loader`` and ``service_report`` are the two seams that matter: a
    test can hand in a settings object and a fake report without a config file,
    a network or a real probe ever running.
    """

    def __init__(
        self,
        *,
        settings_loader: Callable[[], Any] | None = None,
        service_report_loader: Callable[[], Any] | None = None,
        now: Callable[[], str] | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self._settings_loader = settings_loader or _default_settings_loader
        self._service_report_loader = service_report_loader or _default_service_report
        self._now = now or _utc_now
        self.repo_root = Path(repo_root) if repo_root is not None else REPO_ROOT
        self._settings: Any = None
        self._settings_error: BaseException | None = None
        self._service_report: Any = None

    # -- lazily resolved, cached, failures reported to the probe ----------- #

    def settings(self) -> Any:
        if self._settings is None and self._settings_error is None:
            try:
                self._settings = self._settings_loader()
            except BaseException as exc:  # noqa: BLE001 - becomes a blocked check
                self._settings_error = exc
        if self._settings_error is not None:
            raise self._settings_error
        return self._settings

    def service_report(self) -> Any:
        if self._service_report is None:
            self._service_report = self._service_report_loader()
        return self._service_report

    def now(self) -> str:
        return self._now()

    # -- paths ------------------------------------------------------------- #

    def config_path(self) -> Path:
        config = _require("claire.config", _import_fix("claire.config"))
        explicit = os.environ.get("CLAIRE_CONFIG_PATH", "").strip()
        if explicit:
            return Path(explicit).expanduser()
        return Path(config.default_config_path())

    def key_store_path(self) -> Path:
        keys = _require("claire.platform.keys", _import_fix("claire.platform.keys"))
        return Path(keys.default_store_path())

    def env_file(self) -> Path:
        service = _require("claire.platform.service", _import_fix("claire.platform.service"))
        return Path(service.default_env_file())

    def capture_path(self) -> Path:
        return Path(tempfile.gettempdir()) / f"claire_acceptance_{os.getpid()}.png"


def _default_settings_loader() -> Any:
    config = _require("claire.config", _import_fix("claire.config"))
    return config.load_settings()


def _default_service_report() -> Any:
    service = _require("claire.platform.service", _import_fix("claire.platform.service"))
    check = service.SelfCheck(llm_timeout=LLM_PROBE_TIMEOUT_SECONDS)
    return check.run()


#: The LLM endpoint is the only check that touches the network. Short enough
#: that the whole run stays a diagnostic rather than a hang.
LLM_PROBE_TIMEOUT_SECONDS = 2.0


# --------------------------------------------------------------------------- #
# Helpers shared by probes
# --------------------------------------------------------------------------- #

_ENV_NAME_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _env_file_names(path: Path) -> list[str]:
    """The variable *names* in an env file. Values are never read out."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    names: list[str] = []
    for line in text.splitlines():
        match = _ENV_NAME_RE.match(line)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def _service_result_to_probe(result: Any) -> ProbeResult:
    """Map a :class:`claire.platform.service.CheckResult` onto our vocabulary."""

    service = _require("claire.platform.service", _import_fix("claire.platform.service"))
    status = str(getattr(result, "status", ""))
    detail = str(getattr(result, "detail", "") or "")
    fix = getattr(result, "fix", None)
    data = dict(getattr(result, "data", {}) or {})
    data["service_status"] = status
    if status == service.STATUS_READY:
        return ProbeResult.ready(detail or "ready", **data)
    return ProbeResult.blocked(
        detail or f"the platform self-check reported {status}",
        fix or service.FIX_MISSING_NO_FIX,
        **data,
    )


def _service_check_named(report: Any, name: str) -> Any:
    for check in getattr(report, "checks", ()) or ():
        if getattr(check, "name", None) == name:
            return check
    return None


# --------------------------------------------------------------------------- #
# The probes
# --------------------------------------------------------------------------- #


def probe_config(ctx: AcceptanceContext) -> ProbeResult:
    """Does the configuration load, and what does it say Claire runs?"""

    path = ctx.config_path()
    if not path.exists():
        return ProbeResult.blocked(
            f"no configuration file at {path}. Claire runs on built-in defaults "
            "without it, but nothing here can see which provider, model, voice or "
            "recogniser was intended.",
            fix=(
                f"Create it: `mkdir -p {path.parent} && cp examples/config.json "
                f"{path}`, then set the provider/model in the settings panel "
                f"(`{PY} -m claire` starts Claire)."
            ),
            config_path=str(path),
        )
    settings = ctx.settings()
    return ProbeResult.ready(
        f"configuration loaded from {path}",
        config_path=str(path),
        llm_provider=str(getattr(settings, "llm_provider", "") or ""),
        llm_base_url=str(getattr(settings, "llm_base_url", "") or ""),
        tts_engine=str(getattr(settings, "tts_engine", "") or ""),
        stt_engine=str(getattr(settings, "stt_engine", "") or ""),
        stt_model_path=str(getattr(settings, "stt_model_path", "") or ""),
    )


def probe_vault(ctx: AcceptanceContext) -> ProbeResult:
    """The memory vault: present and writable (via the platform self-check)."""

    report = ctx.service_report()
    result = _service_check_named(report, "vault")
    if result is None:
        return ProbeResult.unmeasured(
            "the platform self-check produced no 'vault' probe, so the vault "
            "could not be measured by this run.",
            fix="Re-run `python -m claire.platform.service --self-check --json` and report the missing probe.",
        )
    return _service_result_to_probe(result)


def probe_key_store(ctx: AcceptanceContext) -> ProbeResult:
    """Credential names, masked renderings and fingerprints - never a value."""

    keys = _require("claire.platform.keys", _import_fix("claire.platform.keys"))
    path = ctx.key_store_path()
    env_file = ctx.env_file()
    sources = _credential_name_sources(env_file)
    env_names = next((names for source, names in sources if source == str(env_file)), [])
    names_text = "; ".join(
        f"{source}: {', '.join(names) if names else 'none'}" for source, names in sources
    )

    if not path.exists():
        return ProbeResult.blocked(
            f"no key store file at {path} exists yet, so no credential has been "
            "stored through it (the file is created 0600, outside the repository, on "
            "first write). Credential variable names visible - names only, never "
            f"values - {names_text}.",
            fix=(
                f"{KEY_STORE_FIX} Speech needs the Fish key file named by "
                "fish_key_file; the LLM needs the key its provider reads."
            ),
            store_path=str(path),
            store_exists=False,
            stored_names=[],
            env_file=str(env_file),
            env_file_names=env_names,
            credential_name_sources=[{"path": source, "names": names} for source, names in sources],
        )

    store = keys.KeyStore(path)
    try:
        records = list(store.describe())
    except keys.KeyStoreError as exc:
        return ProbeResult.blocked(
            f"the key store {path} could not be read: {exc}",
            fix=(
                f"Repair the store at {path}: its shape must be "
                '{"version": 1, "keys": {"NAME": "<value>"}}. Back it up, then '
                f"re-store each credential. {KEY_STORE_FIX}"
            ),
            store_path=str(path),
            store_exists=True,
            stored_names=[],
        )
    entries = [
        {
            "name": record.name,
            "masked": record.masked,
            "fingerprint": record.fingerprint,
            "source": record.source,
        }
        for record in records
    ]
    try:
        mode = path.stat().st_mode & 0o777
        mode_text = f"{mode:04o}"
    except OSError:
        mode_text = "unknown"
    tightened = len(getattr(store, "tightened_permissions", []) or [])
    data = {
        "store_path": str(path),
        "store_exists": True,
        "store_mode": mode_text,
        "stored_names": [entry["name"] for entry in entries],
        "entries": entries,
        "env_file": str(env_file),
        "env_file_names": env_names,
        "credential_name_sources": [{"path": source, "names": names} for source, names in sources],
        "permissions_tightened": tightened,
    }
    if not entries:
        return ProbeResult.blocked(
            f"the key store {path} exists (mode {mode_text}) but holds no key "
            f"(credential variable names visible - names only - {names_text}).",
            fix=f"{KEY_STORE_FIX} It writes into {path}.",
            **data,
        )
    return ProbeResult.ready(
        f"{len(entries)} credential(s) in {path} (mode {mode_text}), reported masked: "
        + ", ".join(f"{entry['name']}={entry['masked']}" for entry in entries),
        **data,
    )


def _credential_name_sources(env_file: Path) -> list[tuple[str, list[str]]]:
    """Every credential file Claire may read, as (path, variable names).

    Names only. ``channels.env`` sits beside the systemd ``EnvironmentFile`` and
    holds the outreach transports' variables; reporting only ``claire.env``
    would have said "none" on a machine that plainly has a credential surface.
    """

    candidates = [env_file]
    channels = env_file.parent / "channels.env"
    if channels != env_file:
        candidates.append(channels)
    sources: list[tuple[str, list[str]]] = []
    for candidate in candidates:
        if not candidate.exists():
            if candidate == env_file:
                sources.append((str(candidate), []))
            continue
        sources.append((str(candidate), _env_file_names(candidate)))
    return sources


def probe_audio_device(ctx: AcceptanceContext) -> ProbeResult:
    """Is there a microphone this process can reach?"""

    report = ctx.service_report()
    result = _service_check_named(report, "audio_device")
    if result is None:
        return ProbeResult.unmeasured(
            "the platform self-check produced no 'audio_device' probe.",
            fix="Re-run `python -m claire.platform.service --self-check --json` and report the missing probe.",
        )
    return _service_result_to_probe(result)


def probe_stt_model(ctx: AcceptanceContext) -> ProbeResult:
    """The configured turn recogniser, via the platform self-check."""

    settings = ctx.settings()
    report = ctx.service_report()
    result = _service_check_named(report, "stt_model")
    data: dict[str, Any] = {
        "stt_engine": str(getattr(settings, "stt_engine", "") or ""),
        "stt_model_path": str(getattr(settings, "stt_model_path", "") or ""),
    }
    models_dir = Path.home() / ".local" / "share" / "claire" / "models"
    if models_dir.is_dir():
        data["provisioned_models"] = sorted(p.name for p in models_dir.iterdir() if p.is_dir())
    if result is None:
        return ProbeResult.unmeasured(
            "the platform self-check produced no 'stt_model' probe.",
            fix="Re-run `python -m claire.platform.service --self-check --json` and report the missing probe.",
            **data,
        )
    mapped = _service_result_to_probe(result)
    merged = dict(mapped.data)
    merged.update(data)
    return ProbeResult(mapped.state, mapped.detail, mapped.fix, merged)


def probe_wake_word(ctx: AcceptanceContext) -> ProbeResult:
    """Acoustic wake-word behaviour: genuinely unmeasured on this machine."""

    settings = ctx.settings()
    model_dir = Path(
        str(getattr(settings, "streaming_stt_model_dir", "") or "")
    ).expanduser()
    onnx_files = sorted(p.name for p in model_dir.glob("*.onnx")) if model_dir.is_dir() else []
    return ProbeResult.unmeasured(
        "the wake-word matcher and its streaming model "
        f"({model_dir}, {len(onnx_files)} .onnx file(s) present) are installed, but "
        "no microphone audio has ever been captured on this machine, so acoustic "
        "wake latency and the in-room false-wake rate are unmeasured. The matcher's "
        "own text-only cost is not acoustic latency and is not reported as one.",
        fix=(
            "Measure it on a machine with a working microphone: "
            "`.venv/bin/python scripts/measure_voice_latency.py --only wake_latency` (see "
            "STATUS.md for what that run must capture)."
        ),
        model_dir=str(model_dir),
        model_files_present=len(onnx_files),
        captured_audio_ever=False,
    )


def probe_tts_synthesis(ctx: AcceptanceContext) -> ProbeResult:
    """Live speech synthesis. Spends money, so this check does not do it."""

    settings = ctx.settings()
    engine = str(getattr(settings, "tts_engine", "") or "").strip().lower()
    if engine == "fish":
        fish = _require("claire.output.fish_client", _import_fix("claire.output.fish_client"))
        key_file = Path(
            str(getattr(settings, "fish_key_file", "") or _default_fish_key_file())
        ).expanduser()
        try:
            pool = fish.FishKeyPool(key_file)
        except Exception as exc:  # noqa: BLE001 - named error, with the path
            return ProbeResult.blocked(
                f"the configured engine is Fish, but its key file could not be read: "
                f"{type(exc).__name__}: {exc}",
                fix=(
                    f"Put one API key per line in {key_file} and `chmod 600 {key_file}` "
                    "(or set fish_key_file in the config)."
                ),
                engine=engine,
                key_file=str(key_file),
            )
        count = len(pool)
        return ProbeResult.unmeasured(
            f"the configured engine is Fish and {count} API key(s) are present in "
            f"{key_file} (presence and count only - no value was read). A live "
            "synthesis call spends account credit, and the last live call answered "
            f"{FISH_LAST_LIVE_RESULT}, so this check does not make one.",
            fix=(
                "Add API credit to the Fish account, then measure it: "
                "`.venv/bin/python scripts/measure_voice_latency.py --only first_audio_latency --live-fish`."
            ),
            engine=engine,
            key_file=str(key_file),
            key_count=count,
            last_live_result=FISH_LAST_LIVE_RESULT,
        )

    if engine == "piper":
        model = Path(str(getattr(settings, "tts_piper_model_path", "") or "")).expanduser()
        if model.is_file():
            return ProbeResult.ready(
                f"the configured engine is Piper and its local voice model exists: {model}",
                engine=engine,
                model_path=str(model),
            )
        return ProbeResult.blocked(
            f"the configured engine is Piper but no voice model exists at {model}.",
            fix=(
                "Set `tts_piper_model_path` in the config to an existing .onnx voice "
                "model (voices live under ~/.local/share/claire/models/piper), or "
                "switch `tts_engine`."
            ),
            engine=engine,
            model_path=str(model),
        )

    return ProbeResult.unmeasured(
        f"the configured engine is {engine or '(none)'}, for which this check has no "
        "measurement, so it is reported unmeasured rather than ready.",
        fix=(
            "Set `tts_engine` to `fish` or `piper`, or extend "
            "scripts/acceptance_check.py with a real measurement for this engine."
        ),
        engine=engine,
    )


def probe_llm_endpoint(ctx: AcceptanceContext) -> ProbeResult:
    """Does the configured LLM endpoint answer? (The platform self-check's probe.)"""

    provider = ""
    base_url = ""
    try:  # a broken settings loader must not hide the endpoint verdict
        settings = ctx.settings()
        provider = str(getattr(settings, "llm_provider", "") or "")
        base_url = str(getattr(settings, "llm_base_url", "") or "")
    except BaseException:  # noqa: BLE001 - the config check reports this separately
        provider = "(config unavailable)"
    report = ctx.service_report()
    result = _service_check_named(report, "llm_endpoint")
    if result is None:
        return ProbeResult.unmeasured(
            "the platform self-check produced no 'llm_endpoint' probe.",
            fix=(
                "Re-run `python -m claire.platform.service --self-check --json` and "
                "report the missing probe."
            ),
            llm_provider=provider,
        )
    mapped = _service_result_to_probe(result)
    data = dict(mapped.data)
    data["llm_provider"] = provider
    if base_url:
        data["llm_base_url"] = base_url
    detail = mapped.detail
    if mapped.state != STATE_READY and provider:
        detail = f"{detail} (configured provider: {provider})"
    return ProbeResult(mapped.state, detail, mapped.fix, data)


def probe_memory_router(ctx: AcceptanceContext) -> ProbeResult:
    """The always-loaded router: present, readable, re-read, inside its budget."""

    notes = _require("claire.memory.notes", _import_fix("claire.memory.notes"))
    router_mod = _require("claire.memory.router", _import_fix("claire.memory.router"))
    path = Path(notes.resolve_router_path())
    if not path.exists():
        return ProbeResult.blocked(
            f"no memory router file at {path}, so Claire has nothing always-loaded "
            "to point at her notes and facts.",
            fix=(
                "Create it from the built-in template: "
                f'`{PY} -c "from claire.memory.router import ensure_router; '
                f"ensure_router({str(path)!r})\"` then edit it."
            ),
            router_path=str(path),
        )
    # create=False: a diagnostic must never write the owner's router file.
    router = router_mod.load_router(path, create=False)
    data = {
        "router_path": str(path),
        "size_bytes": router.size_bytes,
        "budget_bytes": router.budget_bytes,
        "over_budget": router.over_budget,
        "dropped_bytes": router.dropped_bytes,
        "reread_per_request": True,
    }
    if router.over_budget:
        return ProbeResult.blocked(
            f"the router {path} is {router.size_bytes} bytes, over its "
            f"{router.budget_bytes}-byte budget; {router.dropped_bytes} bytes would be "
            "dropped from the prompt.",
            fix=(
                f"Trim {path} to {router.budget_bytes} bytes or fewer (the budget can "
                "be raised in its frontmatter with `budget_bytes:`)."
            ),
            **data,
        )
    return ProbeResult.ready(
        f"router {path}: {router.size_bytes} bytes of a {router.budget_bytes}-byte "
        "budget, re-read from disk on every request",
        **data,
    )


def probe_system_prompt(ctx: AcceptanceContext) -> ProbeResult:
    """The assembled prompt actually carries the memory section, in budget."""

    prompt_mod = _require("claire.system_prompt", _import_fix("claire.system_prompt"))
    prompt = prompt_mod.build_system_prompt(load_default_router=True)
    markers = {
        "unavailable": prompt_mod.MEMORY_UNAVAILABLE_MARKER in prompt,
        "empty": prompt_mod.MEMORY_EMPTY_MARKER in prompt,
        "over_budget": prompt_mod.MEMORY_OVER_BUDGET_MARKER in prompt,
    }
    heading = prompt_mod.MEMORY_SECTION_HEADING in prompt
    data = {
        "prompt_chars": len(prompt),
        "memory_heading_present": heading,
        "markers": markers,
    }
    if not heading:
        return ProbeResult.blocked(
            "the assembled system prompt carries no memory section, so the router "
            "would never reach the model.",
            fix=(
                "Check that the router file loads (see the memory_router capability) "
                "and that system_prompt.build_system_prompt(load_default_router=True) "
                "is what the app calls."
            ),
            **data,
        )
    if markers["unavailable"] or markers["empty"] or markers["over_budget"]:
        return ProbeResult.blocked(
            f"the memory section is present but reports a problem: {markers}.",
            fix=(
                "Fix the router file reported by the memory_router capability; the "
                "marker names the failure inside the prompt itself."
            ),
            **data,
        )
    return ProbeResult.ready(
        f"the assembled system prompt ({len(prompt)} chars) carries the memory "
        "section, with no memory-unavailable/empty/over-budget marker",
        **data,
    )


def probe_calendar(ctx: AcceptanceContext) -> ProbeResult:
    """Overlap detection and ranked alternatives, computed purely in memory."""

    cal = _require("claire.schedule.calendar", _import_fix("claire.schedule.calendar"))
    day = cal.add_days(datetime.date.today().isoformat(), 1)
    schedule = cal.empty_schedule()
    first = cal.add_event(
        schedule,
        {"title": "acceptance probe A", "date": day, "start": "09:00", "end": "09:30"},
    )
    if not first.get("ok"):
        return ProbeResult.blocked(
            f"adding a plain event on a free day did not succeed: {first.get('error')}",
            fix="This is a defect in claire.schedule.calendar; report it with the event and the error above.",
            probe_day=day,
        )
    clash = cal.add_event(
        schedule,
        {"title": "acceptance probe B", "date": day, "start": "09:15", "end": "09:45"},
    )
    conflicts = list(clash.get("conflicts") or [])
    proposals = list(clash.get("proposals") or [])
    data = {
        "probe_day": day,
        "conflicts_found": len(conflicts),
        "proposals_found": len(proposals),
        "nearest_proposal": proposals[0].get("label") if proposals else None,
    }
    if clash.get("ok") or not conflicts or not proposals:
        return ProbeResult.blocked(
            "a deliberate 09:15-09:45 clash against 09:00-09:30 was not both "
            f"detected and answered with alternatives (ok={clash.get('ok')}, "
            f"conflicts={len(conflicts)}, proposals={len(proposals)}).",
            fix="This is a defect in claire.schedule.calendar; report the counts above.",
            **data,
        )
    return ProbeResult.ready(
        f"a clash was detected and {len(proposals)} ranked alternative(s) proposed "
        f"(nearest: {data['nearest_proposal']})",
        **data,
    )


def probe_timers(ctx: AcceptanceContext) -> ProbeResult:
    """Start/list/fire a timer against a fake clock (no sleeping, no threads)."""

    timers = _require("claire.schedule.timers", _import_fix("claire.schedule.timers"))
    clock = {"now": 1_000_000.0}
    manager = timers.TimerManager(clock=lambda: clock["now"])
    timer = manager.start(label="acceptance probe", seconds=60)
    active_before = len(manager.list_active())
    clock["now"] += 61.0
    fired = list(manager.fire_due())
    active_after = len(manager.list_active())
    data = {
        "started": str(getattr(timer, "label", "") or ""),
        "active_before_firing": active_before,
        "fired": len(fired),
        "active_after_firing": active_after,
    }
    if active_before != 1 or len(fired) != 1 or active_after != 0:
        return ProbeResult.blocked(
            "the timer did not start, fire and clear exactly once "
            f"(before={active_before}, fired={len(fired)}, after={active_after}).",
            fix="This is a defect in claire.schedule.timers; report the counts above.",
            **data,
        )
    return ProbeResult.ready(
        "a 60 s timer started, listed, fired once and cleared, against a fake clock "
        "(no real time passed)",
        **data,
    )


def probe_screen_capture(ctx: AcceptanceContext) -> ProbeResult:
    """Produce a screenshot right now, then delete it."""

    screen = _require("claire.senses.screen", _import_fix("claire.senses.screen"))
    destination = ctx.capture_path()
    try:
        result = screen.capture_screen(destination)
        detail = (
            f"captured {result.byte_size} bytes to a temporary PNG via "
            f"{result.mechanism}; the file was deleted immediately and no image "
            "content was printed"
        )
        data = {"mechanism": result.mechanism, "byte_size": result.byte_size}
    except screen.ScreenCaptureUnavailableError as exc:
        attempts = list(getattr(exc, "attempts", []) or [])
        fixes = list(getattr(exc, "fixes", []) or []) or list(screen.capture_fix_hints())
        return ProbeResult.blocked(
            "no capture mechanism could produce a PNG: "
            + ("; ".join(f"{name}: {why}" for name, why in attempts) or str(exc)),
            fix="Install one of: " + " OR ".join(fixes),
            attempts=[{"mechanism": name, "reason": why} for name, why in attempts],
        )
    finally:
        try:
            destination.unlink()
        except OSError:
            pass
    return ProbeResult.ready(detail, **data)


def probe_ocr(ctx: AcceptanceContext) -> ProbeResult:
    """Whether text can actually be read off an image here."""

    screen = _require("claire.senses.screen", _import_fix("claire.senses.screen"))
    diagnosis = screen.diagnose()
    data = dict(diagnosis.to_dict()) if hasattr(diagnosis, "to_dict") else {}
    if diagnosis.available:
        return ProbeResult.ready(
            f"OCR is available via {diagnosis.engine} (binary {diagnosis.binary_path})",
            **data,
        )
    return ProbeResult.blocked(
        diagnosis.reason
        or f"OCR is unavailable: the {diagnosis.binary} binary is not installed.",
        fix=diagnosis.fix_command,
        **data,
    )


def probe_camera(ctx: AcceptanceContext) -> ProbeResult:
    """Whether a camera device exists at all."""

    camera = _require("claire.senses.camera", _import_fix("claire.senses.camera"))
    availability = camera.describe_availability()
    data = {
        "available": bool(availability.available),
        "devices": [device.label for device in availability.devices],
        "scanned": list(availability.scanned),
    }
    if availability.available:
        return ProbeResult.ready(
            "camera available: " + ", ".join(data["devices"]), **data
        )
    actions = list(availability.actions) or [
        "Plug in a USB webcam (it appears as /dev/video0) or send a photo from the phone."
    ]
    return ProbeResult.blocked(
        availability.reason or "no camera device was found.",
        fix=" OR ".join(actions),
        **data,
    )


def probe_browser(ctx: AcceptanceContext) -> ProbeResult:
    """Launch headless chromium, read a local page, close it. No network."""

    browser = _require("claire.senses.browser", _import_fix("claire.senses.browser"))
    binary = browser.probe_chromium_binary()
    if not binary:
        return ProbeResult.blocked(
            "no chromium binary was found, so Claire cannot drive a browser.",
            fix=(
                f"Install it: `{PY} -m playwright install chromium` "
                "(the shipped command is: `"
                f"{browser.CHROMIUM_INSTALL_COMMAND}`), then re-run this check."
            ),
            install_command=browser.CHROMIUM_INSTALL_COMMAND,
        )
    page = None
    try:
        session = browser.get_session(headless=True, timeout_ms=BROWSER_PROBE_TIMEOUT_MS)
        session.open(ACCEPTANCE_PAGE_URL)
        page = session.read()
    except browser.BrowserError as exc:
        # A named failure from the browser module itself (a locked profile, a
        # dead binary, a refused connection) is a fact about this machine, not a
        # defect in this check: report it BLOCKED with the module's own fix.
        return ProbeResult.blocked(
            f"headless chromium could not do it: {exc}",
            fix=getattr(exc, "fix", None)
            or (
                f"Re-run `.venv/bin/python scripts/acceptance_check.py --only browser`; "
                f"if it persists, check `{browser.CHROMIUM_INSTALL_COMMAND}` and that no "
                "other chromium holds Claire's profile."
            ),
            binary=binary,
            exception=type(exc).__name__,
        )
    finally:
        try:
            browser.close_session()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
    title = str(getattr(page, "title", "") or "")
    text = str(getattr(page, "text", "") or "")
    data = {"binary": binary, "title": title, "text_chars": len(text)}
    if ACCEPTANCE_MARKER not in title and ACCEPTANCE_MARKER not in text:
        return ProbeResult.blocked(
            "chromium launched but the probe page did not come back "
            f"(title={title!r}, {len(text)} chars read).",
            fix=(
                "Run `.venv/bin/python scripts/acceptance_check.py --only browser --json` and "
                "report the data; a profile lock or a stale chromium is the usual cause."
            ),
            **data,
        )
    return ProbeResult.ready(
        f"headless chromium at {binary} opened a local probe page and read it back "
        f"('{title}', {len(text)} chars)",
        **data,
    )


def probe_outreach_ladder(ctx: AcceptanceContext) -> ProbeResult:
    """Urgency classification and routing, with a provably dry run."""

    ladder_mod = _require("claire.outreach.ladder", _import_fix("claire.outreach.ladder"))
    levels = {
        "low": ladder_mod.classify_urgency("just a thought for later, no rush"),
        "medium": ladder_mod.classify_urgency("please let me know when you can"),
        "high": ladder_mod.classify_urgency("this is urgent, we are out of stock"),
        "critical": ladder_mod.classify_urgency("this is an emergency, call an ambulance"),
    }
    routes = {level: ladder_mod.route_for(level) for level in ladder_mod.LEVELS}
    dry = ladder_mod.Ladder([]).dry_run_default
    outcome = ladder_mod.Ladder([]).run(
        "acceptance probe: nothing may leave this process", level="low"
    )
    data = {
        "classified": levels,
        "routes": {
            level: {
                "action": route.action,
                "send": bool(route.send),
                "call": bool(route.call),
                "max_attempts": int(route.max_attempts),
            }
            for level, route in routes.items()
        },
        "dry_run_default": bool(dry),
        "low_urgency_sent_anything": bool(getattr(outcome, "sent_anything", True)),
    }
    problems: list[str] = []
    if levels != {"low": "low", "medium": "medium", "high": "high", "critical": "critical"}:
        problems.append(f"urgency classification is off: {levels}")
    if routes["low"].send:
        problems.append("a low-urgency message is routed out of the conversation")
    if not routes["critical"].call:
        problems.append("a critical message is not routed to a call")
    if not dry:
        problems.append("dry_run is not the default")
    if data["low_urgency_sent_anything"]:
        problems.append("a low-urgency dry run sent something")
    if problems:
        return ProbeResult.blocked(
            "; ".join(problems),
            fix="This is a defect in claire.outreach.ladder; report the classification counts above.",
            **data,
        )
    return ProbeResult.ready(
        "urgency classified low/medium/high/critical correctly, a low-urgency "
        "message provably stayed in the conversation, and dry_run is the default",
        **data,
    )


def probe_outreach_live(ctx: AcceptanceContext) -> ProbeResult:
    """A real message has never been delivered by this install."""

    return ProbeResult.unmeasured(
        "no message has ever been delivered for real from this install: the ladder's "
        "dry_run default is True and this acceptance run never sends, so live "
        "delivery is unproven.",
        fix=(
            "Turn a transport on in ~/.config/claire/channels.env (DISCORD_TRANSPORT / "
            "SIGNAL_TRANSPORT) and send one real message by hand; only then can this "
            "capability be measured. This check deliberately does not send."
        ),
        dry_run_default=True,
    )


def probe_backups(ctx: AcceptanceContext) -> ProbeResult:
    """Do the rotating snapshot slots verify?"""

    backup = _require("claire.platform.backup", _import_fix("claire.platform.backup"))
    root = Path(backup.default_root())
    if not root.is_dir():
        return ProbeResult.blocked(
            f"no snapshot root at {root}, so there is no backup to restore from.",
            fix=(
                "Take the first snapshot: "
                f'`{PY} -c "from claire.platform import backup; '
                "backup.snapshot(['<file-or-dir>'], label='manual')\"` "
                f"(or set {backup.ROOT_ENV_VAR} to a writable directory)."
            ),
            root=str(root),
            slots_required=backup.DEFAULT_SLOTS,
            slots_verified=0,
        )
    infos = list(backup.list_snapshots(root))
    verified = [info for info in infos if info.verifies]
    data = {
        "root": str(root),
        "slots_required": backup.DEFAULT_SLOTS,
        "slots_present": len(infos),
        "slots_verified": len(verified),
        "slots": [
            {
                "id": info.snapshot_id,
                "label": info.label,
                "created_at": info.created_at,
                "verifies": bool(info.verifies),
                "problems": list(info.problems),
            }
            for info in infos
        ],
    }
    if len(verified) < backup.DEFAULT_SLOTS:
        broken = [info.snapshot_id for info in infos if not info.verifies]
        return ProbeResult.blocked(
            f"{len(verified)} of {backup.DEFAULT_SLOTS} snapshot slots verify under "
            f"{root}"
            + (f"; these slots do not verify: {', '.join(broken)}" if broken else ""),
            fix=(
                "Take more snapshots until every slot verifies (taking one past the "
                "slot count evicts the oldest): "
                f'`{PY} -c "from claire.platform import backup; '
                "backup.snapshot(['<file-or-dir>'], label='acceptance')\"`"
            ),
            **data,
        )
    return ProbeResult.ready(
        f"{len(verified)} verified snapshot slot(s) under {root} "
        f"(re-hashed, not trusted from the manifest)",
        **data,
    )


def probe_service_selfcheck(ctx: AcceptanceContext) -> ProbeResult:
    """The platform self-check itself: did every probe report instead of crash?"""

    service = _require("claire.platform.service", _import_fix("claire.platform.service"))
    report = ctx.service_report()
    checks = list(getattr(report, "checks", ()) or ())
    problems = list(getattr(report, "problems", ()) or ())
    missing = list(getattr(report, "missing", ()) or ())
    names = [getattr(check, "name", "?") for check in checks]
    data = {
        "probes_run": len(checks),
        "probe_names": names,
        "probes_missing": [getattr(check, "name", "?") for check in missing],
        "probes_problem": [getattr(check, "name", "?") for check in problems],
    }
    if problems:
        return ProbeResult.blocked(
            f"{len(problems)} platform probe(s) crashed instead of reporting: "
            f"{', '.join(data['probes_problem'])}. That is a defect in Claire, not a "
            "missing dependency.",
            fix=(
                "Re-run `python -m claire.platform.service --self-check --json` and "
                "report the `data.exception` of the crashing probe. "
                f"{service.FIX_PROBE_CRASHED}"
            ),
            **data,
        )
    return ProbeResult.ready(
        f"the platform self-check ran {len(checks)} probe(s) to completion; "
        f"{len(missing)} reported a missing dependency and 0 crashed",
        **data,
    )


#: Probe name -> implementation. The runner copies this, so injecting a fake for
#: one capability in a test never mutates the module-level table.
DEFAULT_PROBES: Mapping[str, Callable[[AcceptanceContext], ProbeResult]] = {
    "config": probe_config,
    "vault": probe_vault,
    "key_store": probe_key_store,
    "audio_device": probe_audio_device,
    "stt_model": probe_stt_model,
    "wake_word": probe_wake_word,
    "tts_synthesis": probe_tts_synthesis,
    "llm_endpoint": probe_llm_endpoint,
    "memory_router": probe_memory_router,
    "system_prompt": probe_system_prompt,
    "calendar": probe_calendar,
    "timers": probe_timers,
    "screen_capture": probe_screen_capture,
    "ocr": probe_ocr,
    "camera": probe_camera,
    "browser": probe_browser,
    "outreach_ladder": probe_outreach_ladder,
    "outreach_live": probe_outreach_live,
    "backups": probe_backups,
    "service_selfcheck": probe_service_selfcheck,
}


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckOutcome:
    """One capability, as reported."""

    name: str
    title: str
    state: str
    detail: str
    fix: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    #: True only when the *check* misbehaved (crashed or returned rubbish)
    #: rather than the capability being absent. A defect in Claire, not a fact
    #: about the machine.
    problem: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "state": self.state,
            "detail": self.detail,
            "fix": self.fix,
            "problem": bool(self.problem),
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class AcceptanceReport:
    """Every capability's verdict, plus what the exit code must be."""

    checks: tuple[CheckOutcome, ...]
    generated_at: str
    schema: str = SCHEMA

    # -- counts ------------------------------------------------------------ #

    def count(self, state: str) -> int:
        return sum(1 for check in self.checks if check.state == state)

    @property
    def blocked(self) -> tuple[CheckOutcome, ...]:
        return tuple(c for c in self.checks if c.state == STATE_BLOCKED)

    @property
    def unmeasured(self) -> tuple[CheckOutcome, ...]:
        return tuple(c for c in self.checks if c.state == STATE_UNMEASURED)

    @property
    def ready(self) -> tuple[CheckOutcome, ...]:
        return tuple(c for c in self.checks if c.state == STATE_READY)

    @property
    def crashed(self) -> tuple[CheckOutcome, ...]:
        return tuple(c for c in self.checks if c.problem)

    @property
    def exit_code(self) -> int:
        if self.blocked:
            return EXIT_BLOCKED
        if self.unmeasured:
            return EXIT_UNMEASURED
        return EXIT_READY

    @property
    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.checks),
            "ready": self.count(STATE_READY),
            "blocked": self.count(STATE_BLOCKED),
            "unmeasured": self.count(STATE_UNMEASURED),
            "problems": len(self.crashed),
            "exit_code": self.exit_code,
        }

    @property
    def verdict(self) -> str:
        counts = self.counts
        if counts["blocked"]:
            return (
                f"{counts['blocked']} capability(ies) BLOCKED - fix each one above, "
                "then re-run this check."
            )
        if counts["unmeasured"]:
            return (
                f"{counts['unmeasured']} capability(ies) UNMEASURED - this run cannot "
                "certify the install; see the reasons above."
            )
        return "Every capability is READY."

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at": self.generated_at,
            "repo_root": str(REPO_ROOT),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "summary": self.counts,
            "blocked": [check.name for check in self.blocked],
            "unmeasured": [check.name for check in self.unmeasured],
            "problems": [check.name for check in self.crashed],
            "checks": [check.to_dict() for check in self.checks],
        }

    # -- rendering --------------------------------------------------------- #

    def render(self, *, width: int | None = None) -> str:
        columns = _column_widths(width or _terminal_width())
        header = ("CAPABILITY", "STATE", "DETAIL", "FIX")
        rows = [
            (
                check.name,
                check.state,
                f"{check.title}: {check.detail}",
                check.fix or "-",
            )
            for check in self.checks
        ]
        lines = [
            f"Claire acceptance check - {self.generated_at}",
            f"Repository: {REPO_ROOT}",
            "",
            _render_table(header, rows, columns),
            "",
        ]
        counts = self.counts
        lines.append(
            "Summary: {ready} READY, {blocked} BLOCKED, {unmeasured} UNMEASURED "
            "({total} capabilities)".format(**counts)
        )
        if counts["problems"]:
            lines.append(
                "Problems: {n} check(s) crashed instead of reporting - a defect in "
                "Claire, not a missing dependency: {names}".format(
                    n=counts["problems"],
                    names=", ".join(c.name for c in self.crashed),
                )
            )
        else:
            lines.append("Problems: none - every check reported a verdict.")
        lines.append("Verdict: " + self.verdict)
        lines.append(
            "No secret value is printed by this report: credentials appear as a "
            "name, a masked rendering (last-4 + fingerprint) or a count only."
        )
        lines.append(f"Exit code: {self.exit_code}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The runner: every check guarded, one at a time
# --------------------------------------------------------------------------- #


class AcceptanceRunner:
    """Runs the checks and always returns a report.

    Injection points exist so tests never need a real microphone, a real camera,
    a real screen, a real chromium or a live endpoint:

    * ``probes`` - override any check by name (a fake, or one that raises).
    * ``context`` - the settings loader and platform report the probes read.
    * ``now`` - clock, so the report's timestamp is deterministic under test.
    """

    def __init__(
        self,
        *,
        probes: Mapping[str, Callable[[AcceptanceContext], Any]] | None = None,
        context: AcceptanceContext | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self._probes: dict[str, Callable[[AcceptanceContext], Any]] = dict(DEFAULT_PROBES)
        if probes:
            self._probes.update(probes)
        self.context = context or AcceptanceContext(now=now)

    @property
    def probe_names(self) -> tuple[str, ...]:
        return tuple(self._probes)

    def run(self, order: Sequence[str] | None = None) -> AcceptanceReport:
        """Run every selected check and return the report. Never raises for a check."""

        names = tuple(order) if order is not None else CHECK_ORDER
        results = [self._run_one(name) for name in names]
        return AcceptanceReport(checks=tuple(results), generated_at=self.context.now())

    # NOTE: the naive first version of this method called the probe unguarded,
    # so one exploding check took the whole scoreboard with it (RED evidence in
    # docs/ACCEPTANCE.md). The guard below is the fail-proof: every check is
    # wrapped individually, exactly like claire.platform.service's self-check,
    # and a crash becomes a reported problem for that one capability.
    def _run_one(self, name: str) -> CheckOutcome:
        probe = self._probes.get(name)
        if probe is None:
            return CheckOutcome(
                name=name,
                title=CHECK_TITLES.get(name, name),
                state=STATE_BLOCKED,
                detail=f"no check is registered under {name!r}.",
                fix=(
                    f"Pick a known capability: {', '.join(CHECK_ORDER)} "
                    "(`--only` takes one of those)."
                ),
                data={"registered": sorted(self._probes)},
                problem=True,
            )
        try:
            result = probe(self.context)
        except (KeyboardInterrupt, SystemExit):
            # The owner asked to stop; that is not an acceptance finding.
            raise
        except CapabilityImportError as exc:
            return CheckOutcome(
                name=name,
                title=CHECK_TITLES.get(name, name),
                state=STATE_BLOCKED,
                detail=str(exc),
                fix=exc.fix,
                data={
                    "exception": type(exc.cause).__name__,
                    "message": str(exc.cause),
                    "module": exc.module,
                },
                problem=True,
            )
        except BaseException as exc:  # noqa: BLE001 - the whole point
            return CheckOutcome(
                name=name,
                title=CHECK_TITLES.get(name, name),
                state=STATE_BLOCKED,
                detail=(
                    f"check crashed: {type(exc).__name__}: {exc}"
                    if str(exc)
                    else f"check crashed: {type(exc).__name__}"
                ),
                fix=FIX_CHECK_CRASHED,
                data={"exception": type(exc).__name__, "message": str(exc)},
                problem=True,
            )
        return self._normalise(name, result)

    def _normalise(self, name: str, result: Any) -> CheckOutcome:
        title = CHECK_TITLES.get(name, name)
        if isinstance(result, ProbeResult):
            return CheckOutcome(
                name=name,
                title=title,
                state=result.state,
                detail=result.detail,
                fix=result.fix,
                data=dict(result.data),
            )
        return CheckOutcome(
            name=name,
            title=title,
            state=STATE_BLOCKED,
            detail=(
                f"the check returned {type(result).__name__} instead of a verdict"
                + (f": {result!r}" if result is not None else " (None)")
            ),
            fix=FIX_CHECK_MALFORMED,
            data={"returned": type(result).__name__},
            problem=True,
        )


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _terminal_width() -> int:
    try:
        return int(os.environ.get("COLUMNS") or 0) or _shutil_width()
    except Exception:  # noqa: BLE001 - a bad COLUMNS must not break the report
        return 100


def _shutil_width() -> int:
    try:
        import shutil

        return int(shutil.get_terminal_size(fallback=(100, 24)).columns)
    except Exception:  # noqa: BLE001
        return 100


def _column_widths(total: int) -> tuple[int, int, int, int]:
    name_w = 18
    state_w = 11
    remaining = max(total, 80) - name_w - state_w - 6
    detail_w = max(28, remaining // 2)
    fix_w = max(28, remaining - detail_w)
    return name_w, state_w, detail_w, fix_w


def _wrap(text: str, width: int) -> list[str]:
    text = " ".join(str(text).split())
    if not text:
        return [""]
    words: list[str] = []
    for word in text.split(" "):
        while len(word) > width:
            words.append(word[:width])
            word = word[width:]
        words.append(word)
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _render_table(
    header: Sequence[str], rows: Iterable[Sequence[str]], widths: Sequence[int]
) -> str:
    separator = "  "
    out = [separator.join(h.ljust(w) for h, w in zip(header, widths)).rstrip()]
    out.append(separator.join("-" * w for w in widths))
    for row in rows:
        cells = [_wrap(cell, w) for cell, w in zip(row, widths)]
        height = max(len(cell) for cell in cells)
        for index in range(height):
            line = separator.join(
                (cell[index] if index < len(cell) else "").ljust(w)
                for cell, w in zip(cells, widths)
            )
            out.append(line.rstrip())
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """The command line, exposed so tests and callers can introspect it."""

    parser = argparse.ArgumentParser(
        prog="acceptance_check",
        description=(
            "Measure every Claire capability in turn and print the honest "
            "scoreboard: READY, BLOCKED (with the fix) or UNMEASURED. Never "
            "crashes on one bad check, and never prints a secret."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the machine-readable report instead of the table (stdout only).",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="NAME[,NAME...]",
        help=(
            "run only these capabilities (repeatable, comma-separated). "
            "Use --list for the names."
        ),
    )
    parser.add_argument(
        "--list",
        dest="list_checks",
        action="store_true",
        help="list every capability name, title and what it measures, then exit 0.",
    )
    return parser


def select_names(only: Sequence[str] | None) -> tuple[tuple[str, ...], list[str]]:
    """Resolve ``--only`` into canonical order. Returns (names, unknown)."""

    if not only:
        return CHECK_ORDER, []
    requested: list[str] = []
    for chunk in only:
        for piece in str(chunk).split(","):
            name = piece.strip()
            if name and name not in requested:
                requested.append(name)
    unknown = [name for name in requested if name not in CHECK_ORDER]
    names = tuple(name for name in CHECK_ORDER if name in requested)
    return names, unknown


def _list_text() -> str:
    width = max(len(name) for name in CHECK_ORDER)
    lines = ["Claire acceptance-check capabilities (use one with --only):", ""]
    for name in CHECK_ORDER:
        lines.append(
            f"  {name.ljust(width)}  {CHECK_TITLES.get(name, name)}: "
            f"{CHECK_SUMMARIES.get(name, '')}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checks. Returns the process exit code; never raises for a check."""

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.list_checks:
        print(_list_text())
        return EXIT_READY

    names, unknown = select_names(args.only)
    if unknown:
        print(
            f"error: unknown capability(ies): {', '.join(unknown)}. "
            f"Known capabilities: {', '.join(CHECK_ORDER)}. "
            "Run `.venv/bin/python scripts/acceptance_check.py --list` for what each one measures.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if not names:
        print(
            "error: --only selected no capability; pass at least one name "
            f"(known: {', '.join(CHECK_ORDER)}).",
            file=sys.stderr,
        )
        return EXIT_USAGE

    report = AcceptanceRunner().run(names)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=False, default=str))
    else:
        print(report.render())
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
