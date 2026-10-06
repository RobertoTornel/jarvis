"""Signal outreach transport: text messages, plus voice calls when they exist.

This module implements the shared ``Transport`` interface (see
``src/claire/outreach/transport.py``) for Signal.  It is the only transport in
Claire's ladder that can place a *voice call*, so the results it returns must be
readable without ambiguity:

======================  ==================  =========  ======================
what happened           ``delivered``       ``dry_run``  ``error``
======================  ==================  =========  ======================
sent for real           ``True``            ``False``  ``None``
rehearsed (dry run)     ``False``           ``True``   ``None``
failed / impossible     ``False``           ``False``  named reason
======================  ==================  =========  ======================

Exactly those three shapes — the same ones ``transport.SendResult`` validates —
so a failure is never dressed up as a rehearsal and a rehearsal never claims
delivery.  ``action`` is the shared vocabulary: ``"send"`` for a text, ``"call"``
for a voice call, which is what makes the two capabilities distinguishable in a
result.

A dry run never calls the injected sending layer, so it never opens a socket,
spawns ``signal-cli`` or rings a phone.  Live traffic additionally requires
*all* of:

1. ``dry_run=False`` on the call,
2. the live opt-in flag (``SIGNAL_LIVE`` and friends) being set truthy,
3. a sender account *and* a recipient being configured, and
4. ``SIGNAL_TRANSPORT``, when present, holding a recognised live mode token.

Failing any of those yields a **rehearsal**, not a silent send.  Voice calls
have one more gate: the configured backend must actually support placing calls,
otherwise the result says the call was impossible instead of reporting that the
message was delivered by voice.

Secrets (phone numbers, tokens) are never placed in a result, a log record or
an exception message; they appear only as last-4 plus a SHA-256 fingerprint.

Configuration comes from the 0600 env file ``~/.config/claire/channels.env``
(overridable with ``CLAIRE_CHANNELS_ENV``), with real environment variables
layered on top.  Key names actually looked for are listed in
``signal.spec.md``; every one of them is optional, and anything missing
produces a *named* error carrying an actionable fix rather than a no-op.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import os
import re
import shutil
import stat as stat_module
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

__all__ = [
    "ACTION_CALL",
    "ACTION_SEND",
    "DEFAULT_ENV_FILE",
    "DEFAULT_LEVEL_RANK",
    "ERROR_CODES",
    "RECIPIENT_KEYS",
    "SENDER_KEYS",
    "SignalCliSender",
    "SignalConfig",
    "SignalDeviceUnavailable",
    "SignalNotConfigured",
    "SignalSender",
    "SignalSendFailed",
    "SignalTransport",
    "SignalTransportError",
    "SignalVoiceCallUnsupported",
    "load_config",
    "mask_secret",
    "read_env_file",
    "redact_text",
    "resolve_send_result_class",
]

log = logging.getLogger("claire.outreach.signal")

# --------------------------------------------------------------------------
# Named error codes.  Every failure path uses one of these so a caller (or a
# human reading a log) can tell "not configured" from "device missing" from
# "the send was refused", and every one carries an actionable fix.
# --------------------------------------------------------------------------
ERROR_CODES = {
    "SIGNAL_NOT_CONFIGURED": "Signal keys are absent from the channels env file.",
    "SIGNAL_DEVICE_UNAVAILABLE": "The signal-cli binary could not be found.",
    "SIGNAL_LIVE_NOT_ENABLED": "Live sending was requested but not opted into.",
    "SIGNAL_VOICE_CALL_UNSUPPORTED": "This backend cannot place voice calls.",
    "SIGNAL_SEND_FAILED": "The sending backend reported a failure.",
}

DEFAULT_ENV_FILE = Path(
    os.environ.get("CLAIRE_CHANNELS_ENV", "~/.config/claire/channels.env")
).expanduser()

#: Key names searched for, in order.  The first present one wins.
SENDER_KEYS: tuple[str, ...] = (
    "SIGNAL_ACCOUNT",
    "SIGNAL_NUMBER",
    "SIGNAL_PHONE_NUMBER",
    "SIGNAL_ACCOUNT_NUMBER",
    "SIGNAL_SENDER",
)
RECIPIENT_KEYS: tuple[str, ...] = (
    "SIGNAL_RECIPIENT",
    "SIGNAL_TARGET_NUMBER",
    "SIGNAL_TARGET_USER_ID",
    "SIGNAL_TARGET",
    "SIGNAL_CONTACT",
    "SIGNAL_TO",
)
LIVE_KEYS: tuple[str, ...] = ("SIGNAL_LIVE", "SIGNAL_ALLOW_LIVE", "SIGNAL_ENABLE_LIVE")
CALL_KEYS: tuple[str, ...] = ("SIGNAL_VOICE_CALL", "SIGNAL_ALLOW_CALLS", "SIGNAL_CALL_ENABLED")
CLI_KEYS: tuple[str, ...] = ("SIGNAL_CLI", "SIGNAL_CLI_PATH")
CALL_COMMAND_KEYS: tuple[str, ...] = ("SIGNAL_CALL_COMMAND", "SIGNAL_CALL_BINARY")
MODE_KEY = "SIGNAL_TRANSPORT"

#: A mode token must be one of these to count as live-capable.  Anything else
#: (including a rehearsal-ish token, or none at all) keeps the transport in
#: rehearsal: an unrecognised mode is never read as permission to send.
LIVE_MODE_TOKENS = frozenset(
    {"live", "live_send", "send", "real", "production", "prod", "enabled", "on"}
)
TRUTHY = frozenset({"1", "true", "yes", "on", "enable", "enabled", "live", "allow", "allowed"})

#: Key-name markers whose *values* are treated as secrets in free text.
SECRETISH_KEY_MARKERS = (
    "TOKEN",
    "SECRET",
    "KEY",
    "PASSWORD",
    "PASS",
    "SIGNATURE",
    "CREDENTIAL",
    "ACCOUNT",
    "NUMBER",
    "RECIPIENT",
    "TARGET",
    "PHONE",
    "CONTACT",
    "SENDER",
    "USER",
)

#: Values shorter than this never join the redaction set by key name alone, so
#: a boolean-ish value ("on") cannot mangle ordinary prose.
_MIN_EXTRA_SECRET_LEN = 6

_LAST4_LEN = 4
_FP_LEN = 8


# --------------------------------------------------------------------------
# Redaction boundary.  Everything that leaves this module (result fields, log
# records, exception text) goes through here first.
# --------------------------------------------------------------------------
def mask_secret(value: Optional[str]) -> str:
    """Describe a secret as ``present/absent`` plus last-4 and fingerprint.

    The value itself is never returned.  ``mask_secret('+34600111222')`` gives
    something like ``'…1222 (fp 1a2b3c4d)'``.
    """
    if value is None or value == "":
        return "absent"
    text = str(value)
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_FP_LEN]
    if len(text) <= _LAST4_LEN:
        return f"present (fp {fingerprint})"
    return f"…{text[-_LAST4_LEN:]} (fp {fingerprint})"


#: International-style numbers: masked even when unknown to us.
_PHONE_PATTERN = re.compile(r"\+\d[\d\s\-().]{4,}\d")


def redact_text(text: Optional[str], secrets: Sequence[Optional[str]] = ()) -> str:
    """Mask known secrets and phone-shaped numbers inside free text.

    Used on anything a backend hands back (stderr, exception text) before it is
    copied into a result or a log line.
    """
    if not text:
        return ""
    cleaned = str(text)
    for secret in secrets:
        if not secret:
            continue
        raw = str(secret)
        if raw in cleaned:
            cleaned = cleaned.replace(raw, f"[redacted:{mask_secret(raw)}]")
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 6:
            for variant in (digits, digits[-10:], "+" + digits):
                if variant and variant in cleaned:
                    cleaned = cleaned.replace(variant, f"[redacted:{mask_secret(raw)}]")
    cleaned = _PHONE_PATTERN.sub("[redacted:number]", cleaned)
    return cleaned


# --------------------------------------------------------------------------
# The shared SendResult, imported defensively: this module must import and run
# whether or not ``transport.py`` exists yet, and must use the real shared
# class once it does.
# --------------------------------------------------------------------------
@dataclass
class _FallbackSendResult:
    """Field-for-field copy of the shared ``transport.SendResult`` contract."""

    action: str
    channel: str
    delivered: bool
    dry_run: bool
    detail: str
    message_id: Optional[str] = None
    error: Optional[str] = None


REQUIRED_SEND_RESULT_FIELDS = (
    "action",
    "channel",
    "delivered",
    "dry_run",
    "detail",
    "message_id",
    "error",
)

#: The shared action vocabulary (``transport.ACTION_SEND`` / ``ACTION_CALL``).
#: Imported defensively: the strings are the contract either way.
try:  # pragma: no cover - shape depends on the sibling module
    from claire.outreach.transport import ACTION_CALL as _SHARED_ACTION_CALL
    from claire.outreach.transport import ACTION_SEND as _SHARED_ACTION_SEND
except Exception:  # noqa: BLE001 - absent or partially written sibling
    _SHARED_ACTION_SEND, _SHARED_ACTION_CALL = "send", "call"

ACTION_SEND = _SHARED_ACTION_SEND
ACTION_CALL = _SHARED_ACTION_CALL

#: Signal is the *preferred* rung of the ladder (transport.py: "Signal 1").
DEFAULT_LEVEL_RANK = 1

_send_result_class_cache: Optional[type] = None


def _is_compatible_send_result(candidate: Any) -> bool:
    names = getattr(candidate, "__dataclass_fields__", None)
    if not names:
        return False
    return all(name in names for name in REQUIRED_SEND_RESULT_FIELDS)


def resolve_send_result_class(force_reload: bool = False) -> type:
    """Return the shared ``SendResult`` class, falling back to a local copy.

    ``transport.py`` is owned by another module and may not exist (or may be
    mid-write) when this module is imported, so the lookup is lazy and cached.
    """
    global _send_result_class_cache
    if _send_result_class_cache is not None and not force_reload:
        return _send_result_class_cache
    for module_name in ("claire.outreach.transport", "src.claire.outreach.transport"):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - absent or partially written sibling
            continue
        candidate = getattr(module, "SendResult", None)
        if _is_compatible_send_result(candidate):
            _send_result_class_cache = candidate
            return candidate
    log.debug(
        "shared SendResult unavailable; using local fallback (fields identical)"
    )
    _send_result_class_cache = _FallbackSendResult
    return _send_result_class_cache


# --------------------------------------------------------------------------
# Errors.  Every message is a named, actionable sentence.
# --------------------------------------------------------------------------
class SignalTransportError(Exception):
    """Base class; ``code`` is one of :data:`ERROR_CODES`."""

    code = "SIGNAL_SEND_FAILED"

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        if code:
            self.code = code

    @property
    def named_message(self) -> str:
        return f"{self.code}: {self}"


class SignalNotConfigured(SignalTransportError):
    code = "SIGNAL_NOT_CONFIGURED"


class SignalDeviceUnavailable(SignalTransportError):
    code = "SIGNAL_DEVICE_UNAVAILABLE"


class SignalVoiceCallUnsupported(SignalTransportError):
    code = "SIGNAL_VOICE_CALL_UNSUPPORTED"


class SignalSendFailed(SignalTransportError):
    code = "SIGNAL_SEND_FAILED"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file.

    Only the *names* ever escape this module; values stay here.  A missing file
    is not an exception: it is reported by :meth:`SignalConfig.describe`.
    """
    values: dict[str, str] = {}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    except OSError as exc:
        log.warning("could not read channels env file %s: %s", path, exc.strerror)
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _first_present(mapping: Mapping[str, str], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = mapping.get(key)
        if value:
            return value
    return None


@dataclass
class SignalConfig:
    """Resolved Signal settings.

    ``repr`` deliberately exposes only masked forms: a bare ``print(config)``
    must not be able to leak a phone number into a log.
    """

    env_file: Path = DEFAULT_ENV_FILE
    file_values: Mapping[str, str] = field(default_factory=dict, repr=False)
    env_values: Mapping[str, str] = field(default_factory=dict, repr=False)
    file_found: bool = True
    mode_token: Optional[str] = field(default=None, repr=False)
    account: Optional[str] = field(default=None, repr=False)
    recipient: Optional[str] = field(default=None, repr=False)
    live_flag: bool = False
    call_enabled: bool = False
    cli_path: Optional[str] = field(default=None, repr=False)
    call_command: Optional[Sequence[str]] = field(default=None, repr=False)

    # -- presence ---------------------------------------------------------
    @property
    def configured(self) -> bool:
        """True when a sender account *and* a recipient are both known."""
        return bool(self.account) and bool(self.recipient)

    @property
    def transport_key_present(self) -> bool:
        return bool(self.mode_token)

    @property
    def mode_is_live_capable(self) -> bool:
        if self.mode_token is None:
            return True  # no mode key at all: the other gates still apply
        return self.mode_token.strip().lower() in LIVE_MODE_TOKENS

    def live_allowed(self) -> bool:
        """Live traffic needs the opt-in flag *and* full configuration."""
        return self.live_flag and self.configured and self.mode_is_live_capable

    def missing(self) -> list[str]:
        """Names of the settings that are absent (never their values)."""
        missing: list[str] = []
        if not self.file_found:
            missing.append(f"{self.env_file} (file not found)")
        if not self.account:
            missing.append("one of " + ", ".join(SENDER_KEYS))
        if not self.recipient:
            missing.append("one of " + ", ".join(RECIPIENT_KEYS))
        return missing

    def secret_values(self) -> list[str]:
        """Every configured value that must never be echoed back.

        The account and recipient always count.  So does any other configured
        value whose *key* looks credential-shaped and is long enough to be a
        credential (``SIGNAL_SIGNATURE``, ``SIGNAL_TOKEN``, ...) — a token that
        turns up in backend stderr must be masked just like a phone number.
        Short values are skipped so a flag like ``on`` cannot mangle prose.
        """
        found: list[str] = []
        for candidate in (self.account, self.recipient):
            if candidate:
                found.append(candidate)
        for source in (self.file_values, self.env_values):
            for key, value in source.items():
                if not value or len(str(value)) < _MIN_EXTRA_SECRET_LEN:
                    continue
                upper = str(key).upper()
                if any(marker in upper for marker in SECRETISH_KEY_MARKERS):
                    found.append(str(value))
        seen: set[str] = set()
        unique: list[str] = []
        for value in found:
            if value not in seen:
                seen.add(value)
                unique.append(value)
        return unique

    def config_error(self) -> str:
        """A named, actionable sentence describing what is missing."""
        if not self.file_found:
            return (
                f"SIGNAL_NOT_CONFIGURED: channels env file {self.env_file} does not exist; "
                f"create it mode 0600 with SIGNAL_ACCOUNT and one of "
                f"{', '.join(RECIPIENT_KEYS)}"
            )
        missing = ", ".join(self.missing()) or "nothing"
        return (
            f"SIGNAL_NOT_CONFIGURED: Signal is not configured for sending "
            f"(missing: {missing}); add those keys to {self.env_file} (mode 0600), "
            f"or set the equivalent environment variables"
        )

    # -- description ------------------------------------------------------
    def masked(self) -> dict[str, Any]:
        """A reporting view safe to print: no values, only names and masks."""
        return {
            "env_file": str(self.env_file),
            "file_found": self.file_found,
            "keys_present": sorted(set(self.file_values) | set(self.env_values)),
            "mode_token": "present" if self.mode_token else "absent",
            "mode_is_live_capable": self.mode_is_live_capable,
            "account": mask_secret(self.account),
            "recipient": mask_secret(self.recipient),
            "live_flag": self.live_flag,
            "call_enabled": self.call_enabled,
            "cli_path_configured": bool(self.cli_path),
            "call_command_configured": bool(self.call_command),
            "configured": self.configured,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            "SignalConfig(configured={configured}, mode_token={mode}, account={account}, "
            "recipient={recipient}, live_flag={live}, call_enabled={call})".format(
                configured=self.configured,
                mode="present" if self.mode_token else "absent",
                account=mask_secret(self.account),
                recipient=mask_secret(self.recipient),
                live=self.live_flag,
                call=self.call_enabled,
            )
        )


def _as_bool(value: Optional[str]) -> bool:
    return bool(value) and str(value).strip().lower() in TRUTHY


def _split_command(value: Optional[str]) -> Optional[tuple[str, ...]]:
    if not value:
        return None
    import shlex

    try:
        parts = tuple(shlex.split(value))
    except ValueError:
        return None
    return parts or None


def load_config(
    *,
    env_file: Optional[Path | str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> SignalConfig:
    """Resolve Signal settings from the env file plus environment variables."""
    path = Path(env_file) if env_file is not None else DEFAULT_ENV_FILE
    env = dict(os.environ if environ is None else environ)
    file_values = read_env_file(path)
    file_found = Path(path).exists()
    if not file_found:
        log.debug("channels env file not found at %s", path)

    def lookup(keys: Sequence[str], default: Optional[str] = None) -> Optional[str]:
        return _first_present(env, keys) or _first_present(file_values, keys) or default

    cli_path = lookup(CLI_KEYS)
    call_command = lookup(CALL_COMMAND_KEYS)
    return SignalConfig(
        env_file=Path(path),
        file_values=file_values,
        env_values={k: v for k, v in env.items() if k in _WATCHED_KEYS},
        file_found=file_found,
        mode_token=lookup([MODE_KEY]),
        account=lookup(SENDER_KEYS),
        recipient=lookup(RECIPIENT_KEYS),
        live_flag=_as_bool(lookup(LIVE_KEYS)),
        call_enabled=_as_bool(lookup(CALL_KEYS)),
        cli_path=cli_path,
        call_command=_split_command(call_command),
    )


_WATCHED_KEYS = frozenset(
    SENDER_KEYS + RECIPIENT_KEYS + LIVE_KEYS + CALL_KEYS + CLI_KEYS + CALL_COMMAND_KEYS + (MODE_KEY,)
)


def file_permission_warning(path: Path) -> Optional[str]:
    """Warn (by name, never by content) when the env file is not 0600."""
    try:
        mode = stat_module.S_IMODE(Path(path).stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return (
            f"channels env file {path} has mode {mode:03o}; it holds credentials and "
            f"should be 0600 (chmod 600 {path})"
        )
    return None


# --------------------------------------------------------------------------
# The injected sending layer.
# --------------------------------------------------------------------------
class SignalSender(Protocol):
    """Anything that can deliver a Signal text and (maybe) place a call.

    Tests inject a fake here, so no test ever spawns ``signal-cli``, opens a
    socket, or rings a real phone.
    """

    def available(self) -> bool:
        """Whether the underlying device/credential is usable."""

    def unavailable_reason(self) -> Optional[str]:
        """A named, actionable reason when :meth:`available` is False."""

    def supports_calls(self) -> bool:
        """Whether this backend can actually place a voice call."""

    def send_text(self, *, account: str, recipient: str, message: str) -> Optional[str]:
        """Deliver a text; return a backend message id when one is known."""

    def place_call(self, *, account: str, recipient: str, message: str) -> Optional[str]:
        """Place a voice call; raise if the backend cannot."""


def _default_runner(
    cmd: Sequence[str], timeout: float
) -> tuple[int, str, str]:  # pragma: no cover - real subprocess
    proc = subprocess.run(
        list(cmd), capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


class SignalCliSender:
    """Real backend: shells out to ``signal-cli`` (never used by unit tests).

    ``signal-cli`` itself has no voice-call feature, so ``supports_calls()`` is
    False unless a call-capable wrapper is configured via
    ``SIGNAL_CALL_COMMAND``.  Claiming a call happened when it did not is
    exactly the failure mode this transport exists to prevent.
    """

    def __init__(
        self,
        *,
        cli_path: Optional[str] = None,
        call_command: Optional[Sequence[str]] = None,
        runner: Optional[Callable[[Sequence[str], float], tuple[int, str, str]]] = None,
        timeout: float = 30.0,
    ) -> None:
        self._configured_cli = cli_path
        self._call_command = tuple(call_command) if call_command else None
        self._runner = runner or _default_runner
        self._timeout = timeout

    # -- capability -------------------------------------------------------
    def _resolve_binary(self) -> Optional[str]:
        if self._configured_cli:
            candidate = Path(self._configured_cli).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            return None
        return shutil.which("signal-cli")

    def available(self) -> bool:
        return self._resolve_binary() is not None

    def unavailable_reason(self) -> Optional[str]:
        if self.available():
            return None
        if self._configured_cli:
            return (
                f"SIGNAL_DEVICE_UNAVAILABLE: signal-cli at your configured path is not an "
                f"executable file; fix the path in SIGNAL_CLI/SIGNAL_CLI_PATH, or install "
                f"signal-cli and make it executable"
            )
        return (
            "SIGNAL_DEVICE_UNAVAILABLE: signal-cli was not found on PATH; install "
            "signal-cli (https://github.com/AsamK/signal-cli) and register this device, "
            "or set SIGNAL_CLI to its absolute path"
        )

    def supports_calls(self) -> bool:
        return self._call_command is not None

    def call_unavailable_reason(self) -> str:
        return (
            "SIGNAL_VOICE_CALL_UNSUPPORTED: signal-cli cannot place voice calls; "
            "configure SIGNAL_CALL_COMMAND (or SIGNAL_CALL_BINARY) with a call-capable "
            "wrapper, and set SIGNAL_VOICE_CALL=1, or send a text instead"
        )

    # -- sending ----------------------------------------------------------
    def _secrets(self, *values: Optional[str]) -> list[str]:
        return [v for v in values if v]

    def _run(
        self, cmd: Sequence[str], secrets: Sequence[str], action: str
    ) -> tuple[int, str, str]:
        try:
            code, out, err = self._runner(cmd, self._timeout)
        except FileNotFoundError:
            raise SignalDeviceUnavailable(
                "SIGNAL_DEVICE_UNAVAILABLE: the signal-cli command could not be started; "
                "install signal-cli or set SIGNAL_CLI to its absolute path"
            ) from None
        except subprocess.TimeoutExpired:
            raise SignalSendFailed(
                f"SIGNAL_SEND_FAILED: {action} timed out after {self._timeout:g}s; check the "
                f"signal-cli daemon and try again"
            ) from None
        except OSError as exc:
            raise SignalSendFailed(
                f"SIGNAL_SEND_FAILED: {action} could not run ({type(exc).__name__}); check "
                f"that signal-cli is executable"
            ) from None
        return code, out, err

    @staticmethod
    def _message_id(output: str) -> Optional[str]:
        candidates = re.findall(r"\b\d{10,}\b", output or "")
        return candidates[-1] if candidates else None

    def send_text(self, *, account: str, recipient: str, message: str) -> Optional[str]:
        binary = self._resolve_binary()
        if binary is None:
            raise SignalDeviceUnavailable(self.unavailable_reason() or "SIGNAL_DEVICE_UNAVAILABLE")
        cmd = [binary, "-a", account, "send", "-m", message, recipient]
        code, out, err = self._run(cmd, self._secrets(account, recipient), "send_text")
        if code != 0:
            detail = redact_text(err or out, self._secrets(account, recipient))[:200]
            raise SignalSendFailed(
                f"SIGNAL_SEND_FAILED: signal-cli exited {code} while sending the text"
                + (f" ({detail})" if detail else "")
                + "; run `signal-cli -a <account> send -m test <recipient>` by hand to see why"
            )
        return self._message_id(out)

    def place_call(self, *, account: str, recipient: str, message: str) -> Optional[str]:
        if not self._call_command:
            raise SignalVoiceCallUnsupported(self.call_unavailable_reason())
        argv = [
            part.format(account=account, recipient=recipient, message=message)
            for part in self._call_command
        ]
        code, out, err = self._run(argv, self._secrets(account, recipient), "place_call")
        if code != 0:
            detail = redact_text(err or out, self._secrets(account, recipient))[:200]
            raise SignalSendFailed(
                f"SIGNAL_SEND_FAILED: the configured call command exited {code}"
                + (f" ({detail})" if detail else "")
                + "; check SIGNAL_CALL_COMMAND and the calling device"
            )
        return self._message_id(out)


# --------------------------------------------------------------------------
# The transport itself.
# --------------------------------------------------------------------------
class SignalTransport:
    """Signal transport implementing the shared ``Transport`` protocol.

    Attributes match the shared interface: ``name``, ``level_rank``,
    ``available()`` and ``send()``.
    """

    name = "signal"
    level_rank: int = DEFAULT_LEVEL_RANK
    last_resort = False

    def __init__(
        self,
        *,
        config: Optional[SignalConfig] = None,
        sender: Optional[SignalSender] = None,
        env_file: Optional[Path | str] = None,
        environ: Optional[Mapping[str, str]] = None,
        level_rank: int = DEFAULT_LEVEL_RANK,
        call_on_critical: bool = False,
    ) -> None:
        self._config = config if config is not None else load_config(
            env_file=env_file, environ=environ
        )
        if sender is not None:
            self._sender: Optional[SignalSender] = sender
        else:
            # Constructing the default backend touches no device: it only
            # resolves a path lazily when a real send is attempted.
            self._sender = SignalCliSender(
                cli_path=self._config.cli_path, call_command=self._config.call_command
            )
        self.level_rank = level_rank
        self.call_on_critical = bool(call_on_critical)
        self._permission_warning = file_permission_warning(self._config.env_file)
        if self._permission_warning:
            log.warning("%s", self._permission_warning)

    # -- configuration ----------------------------------------------------
    @property
    def config(self) -> SignalConfig:
        return self._config

    def sender(self) -> Optional[SignalSender]:
        return self._sender

    def available(self) -> bool:
        """True when the transport is configured (account + recipient known)."""
        return self._config.configured

    @property
    def unavailable_reason(self) -> Optional[str]:
        """A named reason when :meth:`available` is False, else ``None``.

        The ladder reads this attribute to explain *why* a rung was skipped.
        """
        if self.available():
            return None
        return self._config.config_error()

    def supports_calls(self) -> bool:
        """True only when calls are enabled *and* the backend can place them."""
        if not (self._config.call_enabled and self._config.configured):
            return False
        sender = self._sender
        if sender is None:
            return False
        capability = getattr(sender, "supports_calls", False)
        try:
            return bool(capability() if callable(capability) else capability)
        except Exception:  # noqa: BLE001 - a broken probe never means "yes"
            return False

    @property
    def can_call(self) -> bool:
        """Declared call capability (see ``transport.can_place_call``).

        Note the ladder also treats the mere presence of :meth:`call` as
        call-capable, so the *result* of that step — never this flag alone — is
        what tells a caller whether a call really happened.
        """
        return self.supports_calls()

    def describe(self) -> dict[str, Any]:
        """A reporting view: key names, presence and masks only."""
        info = self._config.masked()
        info["transport"] = self.name
        info["available"] = self.available()
        info["unavailable_reason"] = self.unavailable_reason
        info["can_call"] = self.can_call
        info["permission_warning"] = self._permission_warning
        return info

    # -- result plumbing --------------------------------------------------
    def _result(
        self,
        *,
        action: str,
        delivered: bool,
        dry_run: bool,
        detail: str,
        message_id: Optional[str] = None,
        error: Optional[str] = None,
    ):
        result_class = resolve_send_result_class()
        return result_class(
            action=action,
            channel=self.name,
            delivered=delivered,
            dry_run=dry_run,
            detail=detail,
            message_id=message_id,
            error=error,
        )

    def _failed(self, *, action: str, reason: str, detail: str):
        """A real failure.

        Never ``dry_run=True``: per the shared contract a rehearsal is an
        attempt that was deliberately not made, while a failure means the
        transport could not do the thing at all.  Collapsing the two would let
        an unconfigured transport look like a successful rehearsal.
        """
        return self._result(
            action=action,
            delivered=False,
            dry_run=False,
            detail=detail,
            error=reason,
        )

    def _config_failure(self, *, action: str):
        return self._failed(
            action=action,
            detail="nothing was sent and nothing was rehearsed: Signal is not configured",
            reason=self._config.config_error(),
        )

    def _device_failure(self, *, action: str, reason: str):
        return self._failed(
            action=action,
            detail=(
                "nothing was sent and nothing was rehearsed: the Signal device is unavailable"
            ),
            reason=reason,
        )

    def _unsupported_call(self, *, action: str):
        """Calling is impossible here; say so instead of claiming a call."""
        sender = self._sender
        reason: Optional[str] = None
        if not self._config.call_enabled:
            reason = (
                "SIGNAL_VOICE_CALL_UNSUPPORTED: voice calls are disabled for this transport; "
                "set "
                + "/".join(CALL_KEYS[:2])
                + "=1 in "
                + str(self._config.env_file)
                + " (and set SIGNAL_CALL_COMMAND if the backend cannot place calls)"
            )
        else:
            reason_fn = getattr(sender, "call_unavailable_reason", None)
            if callable(reason_fn):
                try:
                    reason = reason_fn()
                except Exception:  # noqa: BLE001
                    reason = None
        return self._failed(
            action=action,
            detail=(
                "no voice call was placed and none was rehearsed: this transport cannot "
                "place voice calls"
            ),
            reason=redact_text(
                reason
                or (
                    "SIGNAL_VOICE_CALL_UNSUPPORTED: the configured Signal backend cannot place "
                    "voice calls; configure SIGNAL_CALL_COMMAND with a call-capable wrapper, or "
                    "send a text instead"
                ),
                self._secrets(),
            ),
        )

    def _secrets(self) -> list[str]:
        """Every value in this configuration that must be masked in free text."""
        return self._config.secret_values()

    def _device_ready(self, *, action: str):
        """Pre-flight the injected backend; returns a SendResult or None.

        Runs on rehearsals too: a rehearsal on a machine that could never send
        is worth reporting as the failure it is, not as a successful dry run.
        """
        sender = self._sender
        if sender is None:
            return self._device_failure(
                action=action,
                reason=(
                    "SIGNAL_DEVICE_UNAVAILABLE: no Signal sending backend is installed; "
                    "pass a sender/configure SIGNAL_CLI so the transport has a device to use"
                ),
            )
        probe = getattr(sender, "available", None)
        if callable(probe):
            try:
                ok = bool(probe())
            except Exception as exc:  # noqa: BLE001
                return self._device_failure(
                    action=action,
                    reason=(
                        f"SIGNAL_DEVICE_UNAVAILABLE: probing the Signal backend failed "
                        f"({redact_text(str(exc), self._secrets())[:160]}); check the device"
                    ),
                )
            if not ok:
                reason_fn = getattr(sender, "unavailable_reason", None)
                reason = None
                if callable(reason_fn):
                    try:
                        reason = reason_fn()
                    except Exception:  # noqa: BLE001
                        reason = None
                return self._device_failure(
                    action=action,
                    reason=redact_text(
                        reason
                        or (
                            "SIGNAL_DEVICE_UNAVAILABLE: the Signal backend reports it is not "
                            "usable; check signal-cli and its registration"
                        ),
                        self._secrets(),
                    ),
                )
        return None

    def _live_ready(self, dry_run: bool) -> bool:
        """Whether this call may actually leave the machine.

        Only an explicit ``dry_run is False`` counts as asking for a real send:
        ``None``, ``0`` or a truthy non-bool is a rehearsal, because "nothing
        leaves the machine by accident" outranks a loosely-typed caller.
        """
        return (dry_run is False) and self._config.live_allowed()

    def _rehearsal_reason(self, dry_run: bool) -> str:
        if dry_run is True:
            return "dry_run=True was requested"
        if dry_run is not False:
            return (
                f"dry_run was {dry_run!r}, and only an explicit dry_run=False permits live "
                f"sending (the default is a rehearsal)"
            )
        if not self._config.live_flag:
            return (
                "live sending is not opted into: set "
                + "/".join(LIVE_KEYS[:2])
                + "=1 (an explicit opt-in) to allow real sends"
            )
        if not self._config.mode_is_live_capable:
            return (
                "SIGNAL_TRANSPORT does not hold a recognised live mode token; set it to "
                "'live' when you actually intend real sends"
            )
        return "Signal is not fully configured"

    # -- public API -------------------------------------------------------
    def send_text(self, message: str, *, dry_run: bool = True):
        """Deliver, or rehearse, a Signal *text message* (action ``"send"``)."""
        return self._text(message, action=ACTION_SEND, dry_run=dry_run)

    def _rehearsed(self, *, action: str, dry_run: bool, what: str):
        return self._result(
            action=action,
            delivered=False,
            dry_run=True,
            detail=(
                f"rehearsed: nothing left the machine -- no Signal {what} ("
                + self._rehearsal_reason(dry_run)
                + ")"
            ),
        )

    def _text(self, message: str, *, action: str, dry_run: bool):
        if not self.available():
            return self._config_failure(action=action)
        ready = self._device_ready(action=action)
        if ready is not None:
            return ready
        if not self._live_ready(dry_run):
            return self._rehearsed(action=action, dry_run=dry_run, what="text message")
        return self._dispatch_text(message, action=action)

    def _dispatch_text(self, message: str, *, action: str):
        try:
            message_id = self._sender.send_text(  # type: ignore[union-attr]
                account=self._config.account or "",
                recipient=self._config.recipient or "",
                message=message,
            )
        except SignalTransportError as exc:
            return self._result(
                action=action,
                delivered=False,
                dry_run=False,
                detail="the Signal backend refused or failed to send the text",
                error=redact_text(exc.named_message, self._secrets()),
            )
        except Exception as exc:  # noqa: BLE001 - never leak a raw backend error
            return self._result(
                action=action,
                delivered=False,
                dry_run=False,
                detail="the Signal backend raised while sending the text",
                error=(
                    "SIGNAL_SEND_FAILED: the sending backend raised "
                    f"{type(exc).__name__} ({redact_text(str(exc), self._secrets())[:160]}); "
                    "check the signal-cli device and try again"
                ),
            )
        log.info(
            "signal text delivered=%s message_id=%s", True, message_id or "unknown"
        )
        return self._result(
            action=action,
            delivered=True,
            dry_run=False,
            detail="sent for real over Signal (text message)",
            message_id=str(message_id) if message_id is not None else None,
        )

    def place_call(self, message: str, *, dry_run: bool = True):
        """Place, rehearse, or honestly refuse a Signal *voice call*.

        A voice call is a different capability from a text, and this is the only
        transport that has it, so the result is unambiguous:

        * **placed for real** -- ``delivered=True``, ``dry_run=False``, no error;
        * **rehearsed** -- ``delivered=False``, ``dry_run=True``, no error;
        * **impossible** -- ``delivered=False``, ``dry_run=False`` and a named
          ``error`` beginning ``SIGNAL_VOICE_CALL_UNSUPPORTED``.  Nothing is
          reported as delivered, because nothing was placed.
        """
        return self._voice(message, action=ACTION_CALL, dry_run=dry_run)

    def call(self, message: str, *, level: str = "critical", dry_run: bool = True):
        """The ladder's optional voice-call step; result action is ``"call"``.

        Present so ``ladder.can_place_call`` sees a real call capability instead
        of having to guess.  The *result* still says honestly whether a call was
        placed, rehearsed, or impossible.
        """
        return self._voice(message, action=ACTION_CALL, dry_run=dry_run)

    def _voice(self, message: str, *, action: str, dry_run: bool):
        if not self.available():
            return self._config_failure(action=action)
        # Capability before intent: a call that can never be placed is a
        # failure, not a rehearsal, however dry the caller asked to be.
        if not self.supports_calls():
            return self._unsupported_call(action=action)
        ready = self._device_ready(action=action)
        if ready is not None:
            return ready
        if not self._live_ready(dry_run):
            return self._rehearsed(action=action, dry_run=dry_run, what="voice call")
        try:
            call_id = self._sender.place_call(  # type: ignore[union-attr]
                account=self._config.account or "",
                recipient=self._config.recipient or "",
                message=message,
            )
        except SignalTransportError as exc:
            return self._result(
                action=action,
                delivered=False,
                dry_run=False,
                detail="the Signal backend refused or failed to place the voice call",
                error=redact_text(exc.named_message, self._secrets()),
            )
        except Exception as exc:  # noqa: BLE001 - never leak a raw backend error
            return self._result(
                action=action,
                delivered=False,
                dry_run=False,
                detail="the Signal backend raised while placing the voice call",
                error=(
                    "SIGNAL_SEND_FAILED: the calling backend raised "
                    f"{type(exc).__name__} ({redact_text(str(exc), self._secrets())[:160]}); "
                    "check the calling device and try again"
                ),
            )
        log.info("signal voice call placed message_id=%s", call_id or "unknown")
        return self._result(
            action=action,
            delivered=True,
            dry_run=False,
            detail="voice call placed for real over Signal",
            message_id=str(call_id) if call_id is not None else None,
        )

    def send(self, message: str, *, level: str = "medium", dry_run: bool = True):
        """Shared-interface entry point: the **text** rung (action ``"send"``).

        ``level`` is accepted for the interface but does not silently turn a
        text into a call: the ladder drives the voice-call step through
        :meth:`call`, and escalating here as well would call twice on one
        critical message.  Set ``call_on_critical=True`` only when this
        transport is used standalone; even then the fallback detail says plainly
        when a call was impossible and a text went instead.
        """
        urgency = (level or "").strip().lower()
        if urgency == "critical" and self.call_on_critical:
            call_result = self._voice(message, action=ACTION_CALL, dry_run=dry_run)
            if call_result.delivered or call_result.error is None:
                return call_result
            # Calling was impossible: say so, then attempt the text fallback.
            text_result = self._text(message, action=ACTION_SEND, dry_run=dry_run)
            if text_result.ok:
                verb = (
                    "sent a Signal text instead"
                    if text_result.delivered
                    else "rehearsed a Signal text instead, nothing sent"
                )
                return self._result(
                    action=text_result.action,
                    delivered=text_result.delivered,
                    dry_run=text_result.dry_run,
                    detail=(
                        "voice call was impossible ("
                        + (call_result.error or "unsupported")
                        + "); "
                        + verb
                    ),
                    message_id=text_result.message_id,
                )
            return self._failed(
                action=ACTION_CALL,
                detail="the voice call was impossible and the text fallback failed too",
                reason=(
                    (call_result.error or "SIGNAL_VOICE_CALL_UNSUPPORTED")
                    + " | text fallback: "
                    + (text_result.error or "unknown failure")
                ),
            )
        return self._text(message, action=ACTION_SEND, dry_run=dry_run)
