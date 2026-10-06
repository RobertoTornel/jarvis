"""Always-on service plumbing: Claire's own health, reported honestly (R-SERVICE-1).

Claire runs as an always-on background process. When she "seems broken" the
owner is standing at a terminal, not reading a stack trace, and the single most
useful thing this module can produce is a *structured, actionable* answer to
"what is actually wrong?".

Three properties are non-negotiable here, and each one is bought with a test in
``tests/test_platform_service.py``:

* **A crashing probe is a reported problem, never an exception.** Every check
  runs inside :func:`_guarded`, and the guard catches ``BaseException`` (except
  ``KeyboardInterrupt`` / ``SystemExit``) and turns the failure into a
  ``PROBLEM`` :class:`CheckResult` carrying the exception type and message. This
  is the whole point of the module: the diagnostic tool must not be the thing
  that crashes. A naive implementation propagates the exception and the owner
  learns nothing.
* **A missing thing names its fix.** ``MISSING`` always carries a ``fix`` string
  with the command or setting that resolves it. A probe that reports MISSING
  without a fix has the fix auto-filled with an explicit "this is a probe
  defect" note rather than being rendered as an unactionable line.
* **No secret value ever leaves this module.** The report is printed to a
  terminal and written to journald; credentials appear only as present/absent,
  or as last-four plus a short hash (:func:`secret_fingerprint`). Everything
  rendered goes through :func:`claire.utils.redact.redact` at the boundary as a
  backstop. Endpoint URLs are stripped of userinfo and query strings
  (:func:`redact_url`).

Deployment facts live in ``deploy/claire.service`` (a systemd **user** unit -
a system unit cannot reach the microphone, see the comment in that file) and
``deploy/Dockerfile`` (text/tool half only - no audio, no display). This module
is what both of them point at for a health answer:

    python -m claire.platform.service --self-check
    python -m claire.platform.service --self-check --json

Exit codes are honest and stable: ``0`` everything ready, ``1`` something is
missing or broken, ``2`` the command line itself was wrong (argparse).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "CHECK_ORDER",
    "CHECK_TITLES",
    "DEFAULT_ENV_FILE",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "ENV_FILE_ENV_VAR",
    "EXIT_NOT_READY",
    "EXIT_READY",
    "EXIT_USAGE",
    "FIX_PROBE_CRASHED",
    "KEYRING_SERVICE_NAME",
    "STATUS_MISSING",
    "STATUS_PROBLEM",
    "STATUS_READY",
    "CheckResult",
    "HealthReport",
    "SelfCheck",
    "build_parser",
    "default_env_file",
    "main",
    "probe_audio_device",
    "probe_browser",
    "probe_key_store",
    "probe_llm_endpoint",
    "probe_stt_model",
    "probe_tesseract",
    "probe_vault",
    "redact_url",
    "render_report",
    "secret_fingerprint",
    "self_check",
]

# --------------------------------------------------------------------------- #
# Vocabulary. Three statuses, because "absent" and "the probe blew up" are
# different problems with different fixes and must not be flattened together.
# --------------------------------------------------------------------------- #

#: The thing is present and usable.
STATUS_READY = "ready"
#: The thing is genuinely absent. Always paired with an actionable ``fix``.
STATUS_MISSING = "missing"
#: The probe could not answer (crashed, returned nonsense, or the endpoint
#: answered with something unexpected). Kept distinct from MISSING on purpose:
#: a broken probe is a bug in Claire, a missing binary is a fact about the box.
STATUS_PROBLEM = "problem"

_VALID_STATUSES = frozenset({STATUS_READY, STATUS_MISSING, STATUS_PROBLEM})

#: Every check, in the order the owner reads them: the things voice needs
#: first, the optional senses next, and the endpoint last (it is the only one
#: that touches the network).
CHECK_ORDER: tuple[str, ...] = (
    "stt_model",
    "vault",
    "key_store",
    "audio_device",
    "tesseract",
    "browser",
    "llm_endpoint",
)

#: Human titles, so the rendered report is readable without the source.
CHECK_TITLES: Mapping[str, str] = {
    "stt_model": "STT model",
    "vault": "Memory vault",
    "key_store": "Key store",
    "audio_device": "Audio input",
    "tesseract": "Tesseract OCR",
    "browser": "Browser (chromium)",
    "llm_endpoint": "LLM endpoint",
}

EXIT_READY = 0
EXIT_NOT_READY = 1
EXIT_USAGE = 2

#: Where the systemd unit's ``EnvironmentFile=`` points by default. 0600, owned
#: by the user, outside the repository. ``$CLAIRE_ENV_FILE`` overrides it.
ENV_FILE_ENV_VAR = "CLAIRE_ENV_FILE"
DEFAULT_ENV_FILE = Path("~/.config/claire/claire.env")

#: Keychain entry namespace Claire reads keys from (Secret Service / GNOME
#: Keyring). Named here so the report can say *where* it looked.
KEYRING_SERVICE_NAME = "claire"

#: The LLM endpoint probe is the only one that talks to the network; keep its
#: budget short enough that ``--self-check`` stays a diagnostic, not a hang.
DEFAULT_LLM_TIMEOUT_SECONDS = 2.0

#: Handed to a probe that crashed and named no fix of its own.
FIX_PROBE_CRASHED = (
    "This is a defect in Claire's self-check, not a missing dependency: the "
    "probe crashed instead of reporting. Re-run with "
    "`python -m claire.platform.service --self-check --json` and report the "
    "`data.exception` field."
)

#: Handed to a probe that reported MISSING without a fix.
FIX_MISSING_NO_FIX = (
    "No fix was reported by this probe - that is a defect in the probe. "
    "Re-run with `--json` and report the check name."
)

#: Stop the report from ballooning when an exception message is enormous.
_MAX_DETAIL_CHARS = 600


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckResult:
    """One probe's verdict.

    ``status`` is one of :data:`STATUS_READY`, :data:`STATUS_MISSING` or
    :data:`STATUS_PROBLEM`. ``fix`` is required in spirit for anything that is
    not READY (see :func:`_normalise`). ``data`` is machine-readable detail for
    ``--json`` consumers, and must never hold a secret value.
    """

    name: str
    status: str
    detail: str = ""
    fix: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"unknown status {self.status!r}; expected one of "
                f"{sorted(_VALID_STATUSES)}"
            )

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    @property
    def title(self) -> str:
        return CHECK_TITLES.get(self.name, self.name)

    @property
    def label(self) -> str:
        """The status as it is rendered: uppercase, stable, greppable."""
        return self.status.upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class HealthReport:
    """Every check's verdict, plus the one-line summary a human wants."""

    checks: tuple[CheckResult, ...] = ()
    generated_at: str = ""

    # -- lookups ---------------------------------------------------------- #

    def get(self, name: str) -> CheckResult | None:
        for check in self.checks:
            if check.name == name:
                return check
        return None

    @property
    def ready(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == STATUS_READY)

    @property
    def missing(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == STATUS_MISSING)

    @property
    def problems(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == STATUS_PROBLEM)

    @property
    def ok(self) -> bool:
        """True only when *every* probe answered READY."""

        return bool(self.checks) and not self.missing and not self.problems

    @property
    def summary_line(self) -> str:
        verdict = "READY" if self.ok else "NOT READY"
        if not self.checks:
            verdict = "NO CHECKS RAN"
        return (
            f"{len(self.ready)} ready, {len(self.missing)} missing, "
            f"{len(self.problems)} problem - {verdict}"
        )

    # -- serialisation ---------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "ok": self.ok,
            "summary": {
                "ready": len(self.ready),
                "missing": len(self.missing),
                "problem": len(self.problems),
                "total": len(self.checks),
            },
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False, default=str)

    def render(self) -> str:
        """The readable form. Parseable: one check per line, fixes indented."""

        lines = [f"Claire self-check - {self.generated_at or 'unknown time'}"]
        for check in self.checks:
            lines.append(f"[{check.label}] {check.title}: {check.detail}")
            if check.fix:
                lines.append(f"    fix: {check.fix}")
        lines.append(f"Summary: {self.summary_line}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Boundary redaction
# --------------------------------------------------------------------------- #


def redact_url(url: str) -> str:
    """Return ``url`` without userinfo, query or fragment.

    An endpoint URL can carry a credential (``https://user:token@host/v1``) or a
    key in the query string; neither belongs in a report that is printed and
    journalled. The scheme, host, port and path survive so the owner still knows
    which endpoint answered.
    """

    try:
        parts = urlsplit(str(url))
    except ValueError:  # pragma: no cover - urlsplit is very forgiving
        return "<unparseable url>"
    host = parts.hostname or ""
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def secret_fingerprint(value: str | None) -> str:
    """Describe a credential without disclosing it.

    Returns ``"absent"`` for an empty value, otherwise the last four characters
    plus the first 16 hex digits of the SHA-256 of the value. That is enough to
    answer "is this the key I think it is?" (compare the last four) and to tell
    two keys apart in a log, and not enough to use the key. The value itself is
    never included, not even truncated to anything longer than four characters.
    """

    text = "" if value is None else str(value)
    if not text:
        return "absent"
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"present (last4={text[-4:]}, sha256={digest})"


def _safe_text(text: Any) -> str:
    """Scrub secrets out of anything about to be rendered or serialised."""

    raw = "" if text is None else str(text)
    try:
        from ..utils.redact import redact as _redact

        raw = _redact(raw)
    except Exception:  # pragma: no cover - redact is always importable here
        pass
    raw = " ".join(raw.split())
    if len(raw) > _MAX_DETAIL_CHARS:
        raw = raw[: _MAX_DETAIL_CHARS - 1] + "\u2026"
    return raw


def _fix_from_exception(exc: BaseException) -> str | None:
    """Use a named error's own fix, when it carries one.

    Claire's modules raise errors that already know their install command
    (``OcrEngineMissingError.command``, ``SttModelMissingError.hint``, ...). A
    probe that lets one of those through should report *that* fix rather than a
    generic one.
    """

    for attribute in ("fix_command", "command", "install_command", "hint", "fix"):
        candidate = getattr(exc, attribute, None)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


# --------------------------------------------------------------------------- #
# Probe defaults (each one lazily imports its own world)
# --------------------------------------------------------------------------- #


def _settings_value(settings: Any, name: str, default: Any = "") -> Any:
    """Read a settings field defensively.

    A partial settings object (tests, or a config shape that changed underneath
    a probe) must degrade to ``default`` rather than crash the probe.
    """

    value = getattr(settings, name, None)
    if value is None:
        return default
    return value


def _default_settings_loader() -> Any:
    from ..config import load_settings

    return load_settings()


def _default_http_get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """GET ``url``, returning ``(status_code, body)``.

    stdlib only: this is a health check, not a reason to add an HTTP client to
    the diagnostic path. Connection-level failures propagate as ``URLError`` /
    ``OSError`` and are handled by the probe, not swallowed here.
    """

    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 (http/https only)
            status = int(getattr(response, "status", 200) or 200)
            return status, response.read(65536)
    except HTTPError as exc:
        # A 404/401 from the endpoint is an answer, not a transport failure.
        return int(exc.code), b""


def default_env_file() -> Path:
    """The 0600 file the systemd unit loads keys from.

    ``$CLAIRE_ENV_FILE`` wins, then ``~/.config/claire/claire.env`` (honouring
    ``$XDG_CONFIG_HOME``). Returned even when it does not exist, so the fix can
    name the path the owner must create.
    """

    explicit = os.environ.get(ENV_FILE_ENV_VAR)
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "claire" / "claire.env"
    return Path.home() / ".config" / "claire" / "claire.env"


def config_path() -> Path:
    """Where Claire's JSON config lives (mirrors :mod:`claire.config`)."""

    explicit = os.environ.get("CLAIRE_CONFIG_PATH")
    if explicit:
        return Path(explicit).expanduser()
    try:
        from ..config import default_config_path

        return default_config_path()
    except Exception:  # pragma: no cover - config is always importable here
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            return Path(xdg).expanduser() / "claire" / "config.json"
        return Path.home() / ".config" / "claire" / "config.json"


def _env_file_key_names(path: Path) -> list[str]:
    """The *names* of the variables set in an env file. Values are discarded.

    Deliberately returns names only: the caller renders them, and a value must
    never reach the report. Lines that do not look like ``NAME=...`` are
    ignored rather than guessed at.
    """

    names: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return names
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if stripped.startswith("export "):
            name = name[len("export ") :].strip()
        # Drop the value entirely - it is never returned, not even locally.
        if name and name.replace("_", "").isalnum():
            names.append(name)
    return sorted(set(names))


# --------------------------------------------------------------------------- #
# The probes. Signature: (SelfCheck) -> CheckResult.
# A probe may raise; the guard turns that into a PROBLEM.
# --------------------------------------------------------------------------- #


def probe_stt_model(sc: "SelfCheck") -> CheckResult:
    """Is the configured speech-to-text model actually on disk?"""

    settings = sc.settings()
    configured = str(_settings_value(settings, "stt_model_path", "") or "").strip()
    engine = str(_settings_value(settings, "stt_engine", "") or "").strip() or "auto"
    streaming_dir = str(_settings_value(settings, "streaming_stt_model_dir", "") or "").strip()
    cfg = config_path()

    if not configured:
        return CheckResult(
            name="stt_model",
            status=STATUS_MISSING,
            detail=(
                f"stt_model_path is empty in {cfg} (stt_engine={engine}), so no "
                "model is configured and Claire cannot transcribe a turn."
            ),
            fix=(
                f"Set stt_model_path in {cfg} to a Parakeet .nemo file or an ONNX "
                "export directory, then verify with: "
                "python scripts/check_stt_model.py"
            ),
            data={"stt_engine": engine, "streaming_stt_model_dir": streaming_dir},
        )

    path = Path(configured).expanduser()
    if not path.exists():
        return CheckResult(
            name="stt_model",
            status=STATUS_MISSING,
            detail=(
                f"stt_model_path is configured as {path} but that path does not "
                f"exist (stt_engine={engine})."
            ),
            fix=(
                "Fetch the stock model (nvidia/parakeet-tdt-0.6b-v2) or place the "
                f"owner's finetuned export at {path}, then run: "
                "python scripts/check_stt_model.py"
            ),
            data={"stt_engine": engine, "path": str(path)},
        )

    kind = "directory" if path.is_dir() else "file"
    detail = f"{path} ({kind}, stt_engine={engine})"
    if path.is_dir():
        entries = sorted(p.name for p in path.iterdir())
        detail += f", {len(entries)} entries"
    if streaming_dir:
        streaming_path = Path(streaming_dir).expanduser()
        detail += (
            f"; streaming dir {streaming_path} "
            f"{'present' if streaming_path.exists() else 'MISSING (always-on wake is off)'}"
        )
    return CheckResult(
        name="stt_model",
        status=STATUS_READY,
        detail=detail,
        data={
            "stt_engine": engine,
            "path": str(path),
            "kind": kind,
            "streaming_stt_model_dir": streaming_dir,
        },
    )


def probe_vault(sc: "SelfCheck") -> CheckResult:  # noqa: ARG001 - probe contract
    """Is the memory vault present and writable?"""

    from ..memory.notes import resolve_vault_dir

    vault = resolve_vault_dir()
    if not vault.exists():
        return CheckResult(
            name="vault",
            status=STATUS_MISSING,
            detail=f"the memory vault {vault} does not exist yet.",
            fix=f"mkdir -p {vault} && chmod 700 {vault}",
            data={"vault": str(vault)},
        )
    if not vault.is_dir():
        return CheckResult(
            name="vault",
            status=STATUS_MISSING,
            detail=f"{vault} exists but is not a directory.",
            fix=f"Move the file aside and create the directory: mv {vault} {vault}.bak && mkdir -p {vault}",
            data={"vault": str(vault)},
        )
    if not os.access(vault, os.W_OK):
        return CheckResult(
            name="vault",
            status=STATUS_MISSING,
            detail=f"{vault} exists but is not writable by this user, so Claire cannot save a note.",
            fix=f"chmod u+w {vault}   # and check the owner: ls -ld {vault}",
            data={"vault": str(vault)},
        )
    router = vault / "CLAIRE.md"
    detail = f"{vault} (writable)"
    if router.exists():
        detail += f", router file present ({router.stat().st_size} bytes)"
    else:
        detail += f", router file {router} will be created from the template on first run"
    return CheckResult(
        name="vault",
        status=STATUS_READY,
        detail=detail,
        data={"vault": str(vault), "router_present": router.exists()},
    )


def probe_key_store(sc: "SelfCheck") -> CheckResult:  # noqa: ARG001 - probe contract
    """Can Claire actually get at her keys?

    Two stores are checked because Claire reads both: the OS keychain (Secret
    Service / GNOME Keyring, via ``keyring``) and the 0600 env file the systemd
    unit loads. Only *names* and permissions are reported - never a value.
    """

    env_file = default_env_file()
    names: list[str] = []
    mode_text = "absent"
    problems: list[str] = []
    fixes: list[str] = []

    keyring_detail = "keyring not importable"
    keyring_usable = False
    try:
        import keyring

        backend = keyring.get_keyring()
        backend_name = f"{type(backend).__module__}.{type(backend).__name__}"
        priority = getattr(backend, "priority", None)
        # keyring.backends.fail.Keyring has priority 0: nothing is reachable,
        # usually because there is no Secret Service on the session bus.
        keyring_usable = priority is None or float(priority) > 0
        keyring_detail = f"keyring backend {backend_name}"
        if not keyring_usable:
            problems.append(f"the keychain is not reachable ({backend_name})")
            fixes.append(
                "Install and start a Secret Service and run Claire inside the "
                "graphical user session: sudo apt-get install -y gnome-keyring, "
                "then `systemctl --user status gnome-keyring-daemon`"
            )
    except Exception as exc:  # noqa: BLE001 - reported, never propagated
        keyring_detail = f"keyring unavailable: {type(exc).__name__}: {exc}"
        fixes.append("pip install -r requirements.txt   # provides `keyring`")

    if env_file.exists():
        mode_bits: int | None = None
        try:
            mode_bits = env_file.stat().st_mode & 0o777
            mode_text = oct(mode_bits)
        except Exception as exc:  # noqa: BLE001
            mode_text = f"unreadable ({type(exc).__name__})"
        names = _env_file_key_names(env_file)
        if mode_bits is not None and mode_bits & 0o077:
            # The systemd unit file is world-readable; the file it loads keys
            # from must not be, or the keys are effectively public.
            problems.append(f"{env_file} is mode {mode_text} (group/other bits set)")
            fixes.append(f"chmod 600 {env_file}")
    else:
        fixes.append(
            f"Create the key file the systemd unit loads: "
            f"install -m 600 /dev/null {env_file} && "
            f"printf 'OMNIROUTE_API_KEY=...\\n' >> {env_file}   # never commit this file"
        )

    if not keyring_usable and not names:
        return CheckResult(
            name="key_store",
            status=STATUS_MISSING,
            detail=(
                f"no usable key store: {keyring_detail}, and the environment "
                f"file {env_file} is absent or empty."
            ),
            fix="; ".join(fixes) or f"Create {env_file} with mode 0600.",
            data={
                "keyring": keyring_detail,
                "env_file": str(env_file),
                "env_file_mode": mode_text,
                "env_file_names": names,
            },
        )

    if problems:
        return CheckResult(
            name="key_store",
            status=STATUS_PROBLEM,
            detail=(
                f"{keyring_detail}; {env_file} mode {mode_text} with "
                f"{len(names)} variable name(s) [{', '.join(names) or 'none'}]; "
                + "; ".join(problems)
            ),
            fix="; ".join(fixes),
            data={
                "keyring": keyring_detail,
                "env_file": str(env_file),
                "env_file_mode": mode_text,
                "env_file_names": names,
            },
        )

    return CheckResult(
        name="key_store",
        status=STATUS_READY,
        detail=(
            f"{keyring_detail}; {env_file} mode {mode_text} holding "
            f"{len(names)} variable name(s) [{', '.join(names) or 'none'}]"
        ),
        data={
            "keyring": keyring_detail,
            "env_file": str(env_file),
            "env_file_mode": mode_text,
            "env_file_names": names,
        },
    )


def probe_audio_device(sc: "SelfCheck") -> CheckResult:
    """Is there a microphone this process can reach?"""

    settings = sc.settings()
    voice_device = str(_settings_value(settings, "voice_device", "") or "").strip()

    try:
        import sounddevice
    except Exception as exc:
        return CheckResult(
            name="audio_device",
            status=STATUS_MISSING,
            detail=(
                f"the `sounddevice` module is not importable ({type(exc).__name__}: "
                f"{exc}), so no microphone can be opened."
            ),
            fix=(
                "sudo apt-get install -y libportaudio2 && "
                "pip install -r requirements.txt   # provides sounddevice"
            ),
            data={"voice_device": voice_device},
        )

    try:
        try:
            info = sounddevice.query_devices(kind="input")
        except TypeError:
            # Older/duck-typed fakes may not accept `kind`.
            info = sounddevice.query_devices()
            if isinstance(info, (list, tuple)):
                info = info[0] if info else {}
        name = str(dict(info).get("name", "default input")) if info else "default input"
        index = dict(info).get("index", -1) if info else -1
        channels = dict(info).get("max_input_channels", 0) if info else 0
    except Exception as exc:
        return CheckResult(
            name="audio_device",
            status=STATUS_MISSING,
            detail=(
                f"no usable input device: {type(exc).__name__}: {exc}. A systemd "
                "**system** unit cannot reach the user's session bus, so it never "
                "sees the microphone - use the user unit."
            ),
            fix=(
                "Check the device exists (`pactl list short sources` or "
                "`arecord -l`), pick it in `voice_device`, and run Claire under "
                "the USER unit: systemctl --user restart claire  "
                "(deploy/claire.service)"
            ),
            data={"voice_device": voice_device},
        )

    return CheckResult(
        name="audio_device",
        status=STATUS_READY,
        detail=f"{name} (index {index}, {channels} input channel(s), voice_device={voice_device or 'default'})",
        data={
            "device": name,
            "index": index,
            "max_input_channels": channels,
            "voice_device": voice_device,
        },
    )


def probe_tesseract(sc: "SelfCheck") -> CheckResult:  # noqa: ARG001 - probe contract
    """Is the OCR binary the screen sense shells out to installed?"""

    try:
        from ..senses import screen
    except Exception as exc:
        return CheckResult(
            name="tesseract",
            status=STATUS_MISSING,
            detail=f"the screen/OCR module could not be imported ({type(exc).__name__}: {exc}).",
            fix="pip install -r requirements.txt   # provides pytesseract and Pillow",
            data={},
        )

    diagnosis = screen.diagnose()
    data = {
        "binary": getattr(diagnosis, "binary", "tesseract"),
        "binary_path": getattr(diagnosis, "binary_path", None),
        "python_module_present": getattr(diagnosis, "python_module_present", None),
        "package": getattr(diagnosis, "package", ""),
    }
    if getattr(diagnosis, "available", False):
        return CheckResult(
            name="tesseract",
            status=STATUS_READY,
            detail=f"{data['binary']} at {data['binary_path']} ({data['package']})",
            data=data,
        )
    reason = _safe_text(getattr(diagnosis, "reason", "") or "tesseract is not usable here")
    return CheckResult(
        name="tesseract",
        status=STATUS_MISSING,
        detail=reason,
        fix=str(
            getattr(diagnosis, "fix_command", "")
            or f"sudo apt-get install -y {data['package'] or 'tesseract-ocr'}"
        ),
        data=data,
    )


def probe_browser(sc: "SelfCheck") -> CheckResult:  # noqa: ARG001 - probe contract
    """Is the chromium binary Playwright needs on disk?

    Only the binary is probed: launching a browser from a health check would
    take seconds and could leave a profile locked, which is exactly the kind of
    side effect a diagnostic must not have.
    """

    try:
        from ..senses import browser
    except Exception as exc:
        return CheckResult(
            name="browser",
            status=STATUS_MISSING,
            detail=f"the browser module could not be imported ({type(exc).__name__}: {exc}).",
            fix="pip install -r requirements.txt   # provides playwright",
            data={},
        )

    path = browser.probe_chromium_binary()
    install_command = getattr(browser, "CHROMIUM_INSTALL_COMMAND", "playwright install chromium")
    if path:
        return CheckResult(
            name="browser",
            status=STATUS_READY,
            detail=f"chromium binary at {path}",
            data={"binary_path": str(path)},
        )
    return CheckResult(
        name="browser",
        status=STATUS_MISSING,
        detail="no chromium binary found under ~/.cache/ms-playwright, so no page can be opened.",
        fix=f"{install_command}   # ~/.cache/ms-playwright/chromium-*/",
        data={"install_command": install_command},
    )


def probe_llm_endpoint(sc: "SelfCheck") -> CheckResult:
    """Does the configured LLM endpoint answer?

    The only probe that touches the network, and the only one that handles a
    credential. The key travels in an ``Authorization`` header (never in the
    URL) and is reported only as a fingerprint - see
    :func:`secret_fingerprint`.
    """

    settings = sc.settings()
    base_url = str(_settings_value(settings, "llm_base_url", "") or "").strip()
    model = str(_settings_value(settings, "llm_chat_model", "") or "").strip()
    key = str(_settings_value(settings, "llm_api_key", "") or "").strip()
    if not key:
        key = os.environ.get("OMNIROUTE_API_KEY", "").strip()

    if not base_url:
        return CheckResult(
            name="llm_endpoint",
            status=STATUS_MISSING,
            detail=f"llm_base_url is empty in {config_path()}, so there is nowhere to send a turn.",
            fix=(
                f"Set llm_base_url in {config_path()} (OmniRouter defaults to "
                "http://127.0.0.1:20129/v1), or select the ollama provider."
            ),
            data={"model": model},
        )

    safe = redact_url(base_url)
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return CheckResult(
            name="llm_endpoint",
            status=STATUS_MISSING,
            detail=f"llm_base_url={safe!r} is not an http(s) URL.",
            fix=f"Set llm_base_url in {config_path()} to e.g. http://127.0.0.1:20129/v1",
            data={"model": model},
        )

    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    models_url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if key:
        # The header is built here and never rendered; only its presence is.
        headers["Authorization"] = f"Bearer {key}"
    elif not loopback:
        return CheckResult(
            name="llm_endpoint",
            status=STATUS_MISSING,
            detail=(
                f"the non-loopback endpoint {safe} has no API key, so every "
                "request would be rejected."
            ),
            fix=(
                f"Put OMNIROUTE_API_KEY=<key> in {default_env_file()} (mode 0600) "
                f"or set llm_api_key in {config_path()}"
            ),
            data={"url": safe, "model": model, "api_key": "absent"},
        )

    data: dict[str, Any] = {
        "url": safe,
        "models_url": redact_url(models_url),
        "model": model,
        "api_key": secret_fingerprint(key),
    }

    try:
        status_code, body = sc.http_get(models_url, headers, sc.llm_timeout)
    except Exception as exc:
        return CheckResult(
            name="llm_endpoint",
            status=STATUS_MISSING,
            detail=(
                f"nothing answered at {safe}: {type(exc).__name__}: {exc}"
            ),
            fix=(
                f"Start the model server that owns {safe} (for OmniRouter: "
                "systemctl --user start omniroute, or run it as a service), then "
                f"verify: curl -sS {redact_url(models_url)}"
            ),
            data=data,
        )

    if status_code == 200:
        served = _model_count(body)
        detail = f"{safe} answered 200"
        if served is not None:
            detail += f" with {served} model(s)"
        if model:
            detail += f"; configured model={model}"
        return CheckResult(
            name="llm_endpoint",
            status=STATUS_READY,
            detail=detail,
            data={**data, "status_code": status_code, "served_models": served},
        )

    return CheckResult(
        name="llm_endpoint",
        status=STATUS_PROBLEM,
        detail=(
            f"{safe} answered HTTP {status_code} for /models - the endpoint is "
            "reachable but not serving an OpenAI-compatible model list."
        ),
        fix=(
            f"Check llm_base_url in {config_path()} (it needs the /v1 root, not "
            f"/v1/chat/completions) and inspect the server log; try curl -sS "
            f"{redact_url(models_url)}"
        ),
        data={**data, "status_code": status_code},
    )


def _model_count(body: bytes | str | None) -> int | None:
    """How many models the endpoint advertises, when it says."""

    if body is None:
        return None
    try:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        payload = json.loads(text) if text.strip() else {}
    except Exception:
        return None
    if isinstance(payload, dict):
        entries = payload.get("data")
        if isinstance(entries, list):
            return len(entries)
        if isinstance(entries, dict):  # pragma: no cover - some servers nest it
            return len(entries)
    if isinstance(payload, list):  # pragma: no cover - bare list of ids
        return len(payload)
    return None


# --------------------------------------------------------------------------- #
# The runner: every probe guarded, individually
# --------------------------------------------------------------------------- #

#: A probe is any callable taking the runner and returning a CheckResult.
Probe = Callable[["SelfCheck"], CheckResult]

_DEFAULT_PROBES: Mapping[str, Probe] = {
    "stt_model": probe_stt_model,
    "vault": probe_vault,
    "key_store": probe_key_store,
    "audio_device": probe_audio_device,
    "tesseract": probe_tesseract,
    "browser": probe_browser,
    "llm_endpoint": probe_llm_endpoint,
}


class SelfCheck:
    """Runs the probes and always returns a report.

    Injection points exist so tests never need a real microphone, a real
    chromium, a real tesseract or a live endpoint:

    * ``settings`` / ``settings_loader`` - the settings object probes read.
    * ``probes`` - override any probe by name (a fake, or one that raises).
    * ``http_get`` - a ``(url, headers, timeout) -> (status, body)`` callable.
    * ``now`` - clock, so ``generated_at`` is deterministic under test.
    """

    def __init__(
        self,
        *,
        settings: Any | None = None,
        settings_loader: Callable[[], Any] | None = None,
        probes: Mapping[str, Probe] | None = None,
        http_get: Callable[[str, Mapping[str, str], float], tuple[int, bytes]] | None = None,
        now: Callable[[], str] | None = None,
        llm_timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        order: Sequence[str] | None = None,
    ) -> None:
        self._settings = settings
        self._settings_loader = settings_loader or _default_settings_loader
        self._probes: dict[str, Probe] = dict(_DEFAULT_PROBES)
        if probes:
            self._probes.update(probes)
        self.http_get = http_get or _default_http_get
        self._now = now or _utc_now
        self.llm_timeout = float(llm_timeout)
        self._order = tuple(order) if order else CHECK_ORDER

    # -- settings --------------------------------------------------------- #

    def settings(self) -> Any:
        """The settings object, loaded once and cached.

        A loader failure propagates *into the probe*, where the guard turns it
        into a PROBLEM for that check - a broken config must not take the whole
        report down.
        """

        if self._settings is None:
            self._settings = self._settings_loader()
        return self._settings

    # -- running ---------------------------------------------------------- #

    def probe_for(self, name: str) -> Probe | None:
        return self._probes.get(name)

    def run(self, order: Sequence[str] | None = None) -> HealthReport:
        """Run every probe and return the report. Never raises for a probe."""

        names = tuple(order) if order is not None else self._order
        results = [self._run_one(name) for name in names]
        return HealthReport(checks=tuple(results), generated_at=self._now())

    def _run_one(self, name: str) -> CheckResult:
        probe = self._probes.get(name)
        if probe is None:
            return CheckResult(
                name=name,
                status=STATUS_PROBLEM,
                detail=f"no probe is registered under {name!r}.",
                fix=(
                    f"Pick a known check: {', '.join(sorted(self._probes))} "
                    "(`--only` takes one of those)."
                ),
                data={"registered": sorted(self._probes)},
            )
        try:
            result = probe(self)
        except (KeyboardInterrupt, SystemExit):
            # The owner asked to stop; that is not a health finding.
            raise
        except BaseException as exc:  # noqa: BLE001 - the whole point
            return CheckResult(
                name=name,
                status=STATUS_PROBLEM,
                detail=(
                    f"probe crashed: {type(exc).__name__}: {exc}"
                    if str(exc)
                    else f"probe crashed: {type(exc).__name__}"
                ),
                fix=_fix_from_exception(exc) or FIX_PROBE_CRASHED,
                data={"exception": type(exc).__name__, "message": str(exc)},
            )
        return _normalise(name, result)


def _normalise(name: str, result: Any) -> CheckResult:
    """Make a probe's return value safe to render, or say why it was not.

    A probe that returns the wrong shape is a *problem*, not a silent pass -
    and a MISSING result with no fix gets an explicit "probe defect" fix rather
    than being rendered as an unactionable line.
    """

    if result is None:
        return CheckResult(
            name=name,
            status=STATUS_PROBLEM,
            detail="probe returned no result (None).",
            fix=FIX_PROBE_CRASHED,
            data={"exception": "NoneResult"},
        )
    if not isinstance(result, CheckResult):
        return CheckResult(
            name=name,
            status=STATUS_PROBLEM,
            detail=(
                f"probe returned {type(result).__name__}, expected CheckResult."
            ),
            fix=FIX_PROBE_CRASHED,
            data={"exception": f"BadReturn:{type(result).__name__}"},
        )
    if result.name != name:
        return CheckResult(
            name=name,
            status=STATUS_PROBLEM,
            detail=(
                f"probe registered as {name!r} returned a result named "
                f"{result.name!r}."
            ),
            fix=FIX_PROBE_CRASHED,
            data={"exception": "NameMismatch", "reported_name": result.name},
        )

    detail = _safe_text(result.detail)
    fix = _safe_text(result.fix) if result.fix else None
    data = _safe_data(result.data)

    if result.status != STATUS_READY and not fix:
        fix = FIX_MISSING_NO_FIX if result.status == STATUS_MISSING else FIX_PROBE_CRASHED

    if (detail, fix, data) == (result.detail, result.fix, dict(result.data)):
        return result
    return CheckResult(name=name, status=result.status, detail=detail, fix=fix, data=data)


def _safe_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Redact strings inside the machine-readable payload too."""

    safe: dict[str, Any] = {}
    if not isinstance(data, Mapping):  # pragma: no cover - defensive
        return safe
    for key, value in data.items():
        if isinstance(value, str):
            safe[str(key)] = _safe_text(value)
        elif isinstance(value, (list, tuple)):
            safe[str(key)] = [_safe_text(v) if isinstance(v, str) else v for v in value]
        else:
            safe[str(key)] = value
    return safe


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def self_check(**kwargs: Any) -> HealthReport:
    """Convenience wrapper: build a :class:`SelfCheck` and run it."""

    return SelfCheck(**kwargs).run()


# --------------------------------------------------------------------------- #
# Rendering and the command line
# --------------------------------------------------------------------------- #


def render_report(report: HealthReport) -> str:
    """The readable form of a report (HELP output, so stdout is correct here).

    Kept parseable on purpose: one ``[STATUS] Title: detail`` line per check,
    with ``    fix: ...`` indented underneath. ``--json`` exists for callers
    that should not be parsing this.
    """

    return report.render()


def build_parser() -> argparse.ArgumentParser:
    """The command line, exposed so tests can introspect it."""

    parser = argparse.ArgumentParser(
        prog="python -m claire.platform.service",
        description=(
            "Report whether Claire's subsystems are ready - and the fix for "
            "each one that is not. Never crashes: a probe that fails is "
            "reported as a problem."
        ),
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run the health probes and print the report (this is the default).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of the readable form.",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=list(CHECK_ORDER),
        default=None,
        metavar="CHECK",
        help=(
            "Run only this check (repeatable). One of: "
            + ", ".join(CHECK_ORDER)
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_LLM_TIMEOUT_SECONDS,
        help=(
            "Seconds to wait for the LLM endpoint "
            f"(default: {DEFAULT_LLM_TIMEOUT_SECONDS:g})."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m claire.platform.service``.

    ``0`` = everything ready, ``1`` = something is missing or broken (the report
    says what and how to fix it), ``2`` = bad command line (argparse).
    """

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    order = tuple(args.only) if args.only else None
    check = SelfCheck(llm_timeout=args.timeout)
    report = check.run(order)

    if args.json:
        print(report.to_json())
    else:
        print(render_report(report))
    return EXIT_READY if report.ok else EXIT_NOT_READY


if __name__ == "__main__":
    sys.exit(main())
