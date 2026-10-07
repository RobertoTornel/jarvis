"""Discord as a last-resort outreach transport.

Claire reaches the user through a ladder of channels, and Discord sits at the
bottom of it: a low-urgency report stays in the conversation, medium and high go
to Signal first, and Discord is only tried when the channels above it could not
deliver. This module is one rung of that ladder and nothing more. It owns:

* reading the three Discord keys out of the 0600 env file
  (``/home/mana/.config/claire/channels.env``) without ever echoing a value;
* deciding whether the transport is configured at all (:meth:`available`), which
  it answers from configuration alone and never from the network;
* posting exactly one message, through an **injected** HTTP layer so this module
  can be tested without a socket;
* reporting the outcome honestly, in the three shapes the shared
  :class:`claire.outreach.transport.SendResult` keeps apart:

  ==============  ================  =============  ==========================
  outcome         ``delivered``     ``dry_run``     meaning
  ==============  ================  =============  ==========================
  sent for real   ``True``          ``False``      Discord accepted the message
  rehearsed       ``False``         ``True``       nothing left the machine
  failed          ``False``         ``False``      ``error`` names what happened
  ==============  ================  =============  ==========================

  A failed rehearsal is not a fourth outcome to invent: a dry run of an
  unconfigured transport reports a *failure*, because nothing was rehearsed and
  a rehearsal would claim the send would have worked.

``action`` follows the shared contract — it says what the transport *did*
(``"send"``), never how urgent the message was or whether it worked; the outcome
lives in the flags and in ``SendResult.outcome``. Results are built with the
shared ``sent_result`` / ``rehearsed_result`` / ``failed_result`` helpers, so a
sibling module can change the result shape without this file drifting.

Policy that deliberately does **not** live here: which urgency level routes to
which transport, and how often to retry. That is the ladder's job. This module
echoes the ``level`` it was handed and names the failure category; it does not
decide precedence.

Secrets
-------
The token is read, put in an ``Authorization`` header, and otherwise never
handled. It never reaches an exception message, a log line or a returned
structure — :func:`redact` (last four characters plus a SHA-256 fingerprint) is
the only form of it allowed to leave. Response bodies are swept for the token
before they are quoted in an error, because a proxy that echoes request headers
must not turn a Discord 401 into a credential leak.

Named failures
--------------
A missing credential, an unreadable env file, or an unknown
``DISCORD_TRANSPORT`` value is never a silent no-op and never a fake success:
each produces a message naming the key, the file, and the fix.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("claire.outreach.discord")

# --------------------------------------------------------------------------
# The shared interface. transport.py is owned by the ladder agent, so the
# import is defensive: while siblings are written in parallel the module may not
# exist yet, and this file must stay importable either way. The fallback below
# mirrors the shared contract (same fields, same validation, same helpers), but
# the real module always wins once it is on disk, because the ladder checks
# `isinstance(result, transport.SendResult)`.
# --------------------------------------------------------------------------

try:  # pragma: no cover - which branch runs depends on sibling landing order
    from .transport import (  # type: ignore[import-not-found]
        ACTION_CALL,
        ACTION_SEND,
        OUTCOMES,
        SendResult,
        failed_result,
        rehearsed_result,
        sent_result,
    )

    SHARED_INTERFACE = True
except Exception as _import_error:  # pragma: no cover - defensive

    SHARED_INTERFACE = False
    logger.warning(
        "claire.outreach.transport is not importable (%s); Discord is using a "
        "local mirror of the shared SendResult, which will NOT satisfy the "
        "ladder's isinstance check",
        type(_import_error).__name__,
    )

    ACTION_SEND = "send"
    ACTION_CALL = "call"
    OUTCOMES = ("sent", "rehearsed", "failed")

    @dataclass
    class SendResult:  # type: ignore[no-redef]
        """Local mirror of the shared result, field for field."""

        action: str
        channel: str
        delivered: bool
        dry_run: bool
        detail: str
        message_id: Optional[str] = None
        error: Optional[str] = None

        def __post_init__(self) -> None:
            if self.delivered and self.dry_run:
                raise ValueError("a rehearsal cannot claim delivery")
            if self.delivered and self.error:
                raise ValueError("a delivered result cannot carry an error")
            if not self.delivered and not self.dry_run and not self.error:
                raise ValueError("a failed result must name its reason")

        @property
        def outcome(self) -> str:
            if self.delivered:
                return "sent"
            if self.dry_run:
                return "rehearsed"
            return "failed"

        @property
        def ok(self) -> bool:
            return self.error is None and (self.delivered or self.dry_run)

        @property
        def is_failure(self) -> bool:
            return not self.ok

        def summary(self) -> str:
            if self.delivered:
                return f"{self.channel}: sent ({self.action})"
            if self.dry_run:
                return f"{self.channel}: rehearsed, nothing sent ({self.action})"
            return f"{self.channel}: failed ({self.action}): {self.error}"

    def sent_result(
        channel: str,
        action: str = ACTION_SEND,
        detail: str = "",
        *,
        message_id: Optional[str] = None,
    ) -> SendResult:
        return SendResult(
            action=action,
            channel=channel,
            delivered=True,
            dry_run=False,
            detail=detail or "sent",
            message_id=message_id,
        )

    def rehearsed_result(
        channel: str, action: str = ACTION_SEND, detail: str = ""
    ) -> SendResult:
        return SendResult(
            action=action,
            channel=channel,
            delivered=False,
            dry_run=True,
            detail=detail or "dry run: nothing was sent",
        )

    def failed_result(
        channel: str, reason: str, action: str = ACTION_SEND, detail: str = ""
    ) -> SendResult:
        if not reason or not str(reason).strip():
            raise ValueError("failed_result requires a named reason")
        return SendResult(
            action=action,
            channel=channel,
            delivered=False,
            dry_run=False,
            detail=detail or f"failed: {reason}",
            error=str(reason),
        )


__all__ = [
    "CHANNELS_ENV_PATH",
    "ENV_PATH_OVERRIDE_VAR",
    "CHANNEL_NAME",
    "LEVEL_RANK",
    "DEFAULT_API_BASE",
    "MAX_MESSAGE_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "ACTION_SEND",
    "ACTION_CALL",
    "OUTCOMES",
    "SHARED_INTERFACE",
    "CATEGORY_SENT",
    "CATEGORY_REHEARSED",
    "CATEGORY_NOT_CONFIGURED",
    "CATEGORY_CREDENTIAL_REJECTED",
    "CATEGORY_RATE_LIMITED",
    "CATEGORY_SERVER_ERROR",
    "CATEGORY_API_ERROR",
    "CATEGORY_TRANSPORT_ERROR",
    "CATEGORY_INVALID_REQUEST",
    "DiscordError",
    "DiscordEnvFileError",
    "DiscordEnvFileMissingError",
    "DiscordEnvFilePermissionsError",
    "DiscordConfigError",
    "DiscordCredentialRejectedError",
    "DiscordRateLimitedError",
    "DiscordServerError",
    "DiscordApiError",
    "DiscordTransportError",
    "DiscordConfig",
    "DiscordTransport",
    "SendResult",
    "fingerprint",
    "redact",
    "redact_text",
    "parse_env_file",
    "load_config",
    "category_of",
]

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: The env file the operator keeps their channel credentials in. 0600, outside
#: every repository, never copied into one.
CHANNELS_ENV_PATH = Path("/home/mana/.config/claire/channels.env")

#: An env var that may point somewhere else, for tests and for a machine that
#: keeps its credentials elsewhere. Not a secret; the file it names is 0600.
ENV_PATH_OVERRIDE_VAR = "CLAIRE_CHANNELS_ENV"

#: The stable channel id used in every result and every log line.
CHANNEL_NAME = "discord"

#: Position in the urgency ladder. ``None`` is the shared contract's mark for
#: "not an ordinary rung": Signal is rank 1 and goes first, Discord is
#: last-resort only and is never tried while any preferred rung can deliver.
LEVEL_RANK: Optional[int] = None

DEFAULT_API_BASE = "https://discord.com/api/v10"
DEFAULT_TIMEOUT_SECONDS = 15.0

#: A ceiling on the HTTP timeout. A send that hangs for ten minutes holds the
#: ladder's turn open; the caller gets a named failure long before that.
MAX_TIMEOUT_SECONDS = 60.0

#: Discord hard-caps a message at 2000 characters. Checked locally so an
#: oversized message fails with the real reason instead of a generic HTTP 400.
MAX_MESSAGE_CHARS = 2000

#: A user-token endpoint rejects the stock ``python-requests`` user agent, so
#: the default names a browser. Configurable for whoever needs otherwise.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TOKEN_KEY = "DISCORD_SELFBOT_TOKEN"
TARGET_USER_KEY = "DISCORD_TARGET_USER_ID"
TARGET_CHANNEL_KEY = "DISCORD_TARGET_CHANNEL_ID"
TRANSPORT_KEY = "DISCORD_TRANSPORT"

#: env-file modes are grouped the way an operator thinks about them: live,
#: rehearse only, or switched off.
LIVE_MODES = frozenset({"selfbot", "live", "user", "user-token", "usertoken"})
DRY_MODES = frozenset({"dryrun", "dry-run", "dry_run", "rehearse", "test"})
OFF_MODES = frozenset({"off", "false", "0", "no", "none", "disabled"})

MODE_LIVE = "live"
MODE_DRY = "dry"
MODE_OFF = "off"
MODE_UNKNOWN = "unknown"

#: Stable, machine-readable failure categories. Each error string begins with
#: ``[<category>]`` so the ladder can tell a rejected credential (do not retry)
#: from a rate limit (back off and retry) from a server fault (retry later).
CATEGORY_SENT = "sent"
CATEGORY_REHEARSED = "rehearsed"
CATEGORY_NOT_CONFIGURED = "not_configured"
CATEGORY_CREDENTIAL_REJECTED = "credential_rejected"
CATEGORY_RATE_LIMITED = "rate_limited"
CATEGORY_SERVER_ERROR = "server_error"
CATEGORY_API_ERROR = "api_error"
CATEGORY_TRANSPORT_ERROR = "transport_error"
CATEGORY_INVALID_REQUEST = "invalid_request"

_REDACTED_TAIL_CHARS = 4
_FINGERPRINT_CHARS = 8
_BODY_EXCERPT_CHARS = 200


# --------------------------------------------------------------------------
# Named errors. No constructor here is ever handed a token value.
# --------------------------------------------------------------------------


class DiscordError(Exception):
    """Base class for every Discord transport failure.

    A sibling of :class:`claire.output.fish_client.FishError` rather than a
    subclass: speaking and reaching the user fail for different reasons, and
    handling one must not silently swallow the other.
    """


class DiscordEnvFileError(DiscordError):
    """Base class for problems with the channels env file. Carries the path."""

    def __init__(self, path: Any, detail: str = "") -> None:
        self.path = str(path)
        super().__init__(
            f"Discord channel env file {self.path!r} is unusable: "
            f"{detail or 'no detail'}"
        )


class DiscordEnvFileMissingError(DiscordEnvFileError):
    """The env file does not exist or is not a regular file."""


class DiscordEnvFilePermissionsError(DiscordEnvFileError):
    """The env file is readable by group or others; it must be 0600."""


class DiscordConfigError(DiscordError):
    """A required key is missing or holds an unusable value.

    The message names the key, the file, and the fix.
    """


class DiscordCredentialRejectedError(DiscordError):
    """HTTP 401/403: Discord rejected the credential or forbade the target."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        super().__init__(message or f"HTTP {status}: credential rejected")


class DiscordRateLimitedError(DiscordError):
    """HTTP 429: back off before trying again."""

    def __init__(
        self, status: int = 429, message: str = "", retry_after: Optional[str] = None
    ) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(message or f"HTTP {status}: rate limited")


class DiscordServerError(DiscordError):
    """HTTP 5xx: Discord's side failed; nothing about the message is wrong."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        super().__init__(message or f"HTTP {status}: server-side failure")


class DiscordApiError(DiscordError):
    """A non-2xx status that is neither a rejection nor a rate limit."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        super().__init__(message or f"HTTP {status}: request refused")


class DiscordTransportError(DiscordError):
    """The HTTP layer itself failed (DNS, TLS, timeout, connection reset)."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or "the HTTP layer failed")


# --------------------------------------------------------------------------
# Redaction. The only export path for a secret.
# --------------------------------------------------------------------------


def fingerprint(value: str) -> str:
    """A stable, non-reversible handle for a secret.

    Eight hex characters of SHA-256: enough to tell two tokens apart in a log,
    useless for recovering either one. Deliberately the same shape as
    :func:`claire.output.fish_client.fingerprint` so an operator reading both
    logs sees one convention.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_FINGERPRINT_CHARS]


def redact(value: str) -> str:
    """The only form of the token that may leave this module.

    Last four characters plus a fingerprint. A value shorter than eight
    characters gets no tail at all: showing the last four of a six-character
    secret would be most of the secret.
    """
    if not value:
        return "<empty>"
    tag = fingerprint(value)
    if len(value) < 8:
        return f"<redacted:{tag}>"
    return f"…{value[-_REDACTED_TAIL_CHARS:]}:{tag}"


def redact_text(text: str, secrets: Sequence[str]) -> str:
    """Replace every occurrence of every secret in ``text`` with its redaction.

    Applied to response bodies before they are quoted anywhere, so an upstream
    that echoes the ``Authorization`` header cannot smuggle the token into a
    log line or an error string.
    """
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, redact(secret))
    return result


def category_of(result: SendResult) -> Optional[str]:
    """The machine-readable category of a result, or ``None`` if unrecognised.

    Reads the ``[<category>]`` prefix every error string carries, so the ladder
    can branch on the failure kind without parsing prose. A delivered result is
    ``sent`` and a clean rehearsal is ``rehearsed``.
    """
    if result.delivered:
        return CATEGORY_SENT
    if result.error:
        stripped = result.error.lstrip()
        if stripped.startswith("["):
            end = stripped.find("]")
            if end > 0:
                return stripped[1:end]
        return None
    if result.dry_run:
        return CATEGORY_REHEARSED
    return None


# --------------------------------------------------------------------------
# The env file
# --------------------------------------------------------------------------


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_file(path: Any, *, require_private_mode: bool = True) -> Dict[str, str]:
    """Read ``KEY=VALUE`` lines out of the channels env file.

    A missing file, a non-file, or a file readable by group/others raises a
    named error that carries the path and the fix. Blank lines and ``#``
    comments are skipped; a leading ``export`` and surrounding quotes are
    stripped. Nothing here logs the values, deliberately: the whole point of the
    file is that it holds a token, and a debug line is forever.
    """
    file_path = Path(path).expanduser()

    if not file_path.exists():
        raise DiscordEnvFileMissingError(
            file_path,
            f"no such file at {str(file_path)!r}; create it with "
            f"{TRANSPORT_KEY}=selfbot, {TOKEN_KEY}=<your Discord user token> and "
            f"{TARGET_USER_KEY}=<the numeric user id to DM>, then keep it private "
            f"(chmod 600 {file_path})",
        )
    if not file_path.is_file():
        raise DiscordEnvFileMissingError(
            file_path, "path exists but is not a regular file"
        )

    if require_private_mode and os.name == "posix":
        try:
            mode = file_path.stat().st_mode & 0o777
        except OSError as exc:
            raise DiscordEnvFileMissingError(
                file_path, f"cannot stat ({exc.__class__.__name__})"
            ) from exc
        if mode & 0o077:
            raise DiscordEnvFilePermissionsError(
                file_path,
                f"mode is {mode:04o}; a file holding a channel token must be 0600 "
                f"(run: chmod 600 {file_path})",
            )

    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DiscordEnvFileMissingError(
            file_path, f"cannot read ({exc.__class__.__name__})"
        ) from exc

    values: Dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key:
            continue
        values[key] = _strip_quotes(value.strip())
    return values


def _mode_kind(raw: str) -> str:
    if not raw:
        # Unset means the documented default: a live selfbot.
        return MODE_LIVE
    normalised = raw.strip().lower()
    if normalised in LIVE_MODES:
        return MODE_LIVE
    if normalised in DRY_MODES:
        return MODE_DRY
    if normalised in OFF_MODES:
        return MODE_OFF
    return MODE_UNKNOWN


@dataclass(frozen=True)
class DiscordConfig:
    """What the env file said, with the token kept out of every rendering.

    ``token`` is ``repr=False`` and :meth:`__repr__` is written by hand, because
    a dataclass repr that includes the token turns every ``logger.debug("%r")``
    and every failing assertion message into a credential disclosure.
    """

    token: str = field(repr=False, default="")
    target_user_id: Optional[str] = None
    target_channel_id: Optional[str] = None
    transport_mode: str = "selfbot"
    api_base: str = DEFAULT_API_BASE
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    source: Optional[str] = None

    # -- derived -----------------------------------------------------------

    @property
    def mode_kind(self) -> str:
        """``live``, ``dry``, ``off`` or ``unknown``."""
        return _mode_kind(self.transport_mode)

    @property
    def redacted_token(self) -> str:
        """The token as it may appear in a log line: last four plus fingerprint."""
        return redact(self.token)

    @property
    def target_description(self) -> str:
        """Which recipient this transport posts to, for a detail line."""
        if self.target_channel_id:
            return f"channel id {self.target_channel_id}"
        if self.target_user_id:
            return f"user id {self.target_user_id}"
        return "no configured recipient"

    def __repr__(self) -> str:
        return (
            f"DiscordConfig(mode={self.transport_mode!r}, token={self.redacted_token}, "
            f"target_user_id={self.target_user_id!r}, "
            f"target_channel_id={self.target_channel_id!r}, "
            f"api_base={self.api_base!r}, timeout={self.timeout!r}, "
            f"source={self.source!r})"
        )

    # -- readiness ---------------------------------------------------------

    def blockers(self) -> Tuple[str, ...]:
        """Every reason this transport cannot send, each named and actionable.

        Empty tuple means "configured". Never touches the network, and never
        prints a value read out of the env file — an unrecognised
        ``DISCORD_TRANSPORT`` is described, not quoted, in case the operator
        pasted the token into the wrong line.
        """
        problems: List[str] = []
        where = self.source or str(CHANNELS_ENV_PATH)

        if self.mode_kind == MODE_OFF:
            problems.append(
                f"[{CATEGORY_NOT_CONFIGURED}] {TRANSPORT_KEY}=off in {where}: "
                f"Discord is switched off by configuration. Set "
                f"{TRANSPORT_KEY}=selfbot to turn it back on."
            )
        elif self.mode_kind == MODE_UNKNOWN:
            problems.append(
                f"[{CATEGORY_NOT_CONFIGURED}] {TRANSPORT_KEY} in {where} holds an "
                f"unrecognised value (not printed here in case a secret was pasted "
                f"into the wrong line). Expected one of: selfbot, dryrun, off."
            )

        if not self.token.strip():
            problems.append(
                f"[{CATEGORY_NOT_CONFIGURED}] {TOKEN_KEY} is not set in {where}: add "
                f"a line '{TOKEN_KEY}=<your Discord user token>' and keep the file "
                f"0600 (chmod 600 {where}). Without it no Discord message can be "
                f"sent."
            )

        if not (self.target_user_id or "").strip() and not (
            self.target_channel_id or ""
        ).strip():
            problems.append(
                f"[{CATEGORY_NOT_CONFIGURED}] neither {TARGET_USER_KEY} nor "
                f"{TARGET_CHANNEL_KEY} is set in {where}: add "
                f"'{TARGET_USER_KEY}=<the numeric Discord user id to DM>' (or a "
                f"channel id) so the transport knows where to post."
            )

        return tuple(problems)

    @property
    def configured(self) -> bool:
        return not self.blockers()

    def require_configured(self) -> None:
        """Raise :class:`DiscordConfigError` unless every blocker is gone."""
        problems = self.blockers()
        if problems:
            raise DiscordConfigError(" ".join(problems))


def _default_env_path() -> Path:
    override = os.environ.get(ENV_PATH_OVERRIDE_VAR, "").strip()
    return Path(override).expanduser() if override else CHANNELS_ENV_PATH


def load_config(
    path: Any = None,
    *,
    require_private_mode: bool = True,
    strict: bool = False,
    api_base: str = DEFAULT_API_BASE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> DiscordConfig:
    """Build a :class:`DiscordConfig` from the env file.

    Missing keys are **not** fatal here, because a transport must be
    constructible in order to answer ``available() == False`` and explain why.
    Pass ``strict=True`` (or call :meth:`DiscordConfig.require_configured`) where
    a half-configured transport must stop the caller instead.
    """
    file_path = Path(path).expanduser() if path is not None else _default_env_path()
    values = parse_env_file(file_path, require_private_mode=require_private_mode)

    config = DiscordConfig(
        token=values.get(TOKEN_KEY, ""),
        target_user_id=values.get(TARGET_USER_KEY) or None,
        target_channel_id=values.get(TARGET_CHANNEL_KEY) or None,
        transport_mode=values.get(TRANSPORT_KEY, "selfbot") or "selfbot",
        api_base=api_base,
        timeout=timeout,
        user_agent=user_agent,
        source=str(file_path),
    )
    if strict:
        config.require_configured()
    return config


# --------------------------------------------------------------------------
# The transport
# --------------------------------------------------------------------------


def _status_of(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _json_body(response: Any) -> Optional[Mapping[str, Any]]:
    try:
        payload = response.json()
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _body_excerpt(response: Any, secrets: Sequence[str]) -> str:
    """A short, single-line, secret-free quote of a response body."""
    text = getattr(response, "text", "")
    if not isinstance(text, str) or not text:
        payload = _json_body(response)
        text = repr(payload) if payload is not None else ""
    text = redact_text(text, secrets)
    text = " ".join(text.split())
    return text[:_BODY_EXCERPT_CHARS]


def _retry_after(response: Any) -> Optional[str]:
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    return str(value) if value else None


class DiscordTransport:
    """One rung of the outreach ladder: Discord, as the last resort.

    The HTTP layer is injected (``session`` or ``http``) so that tests and dry
    runs never open a socket. Anything with a
    ``post(url, *, json, headers, timeout) -> response`` method works, including
    a plain function; ``requests.Session`` is the default.

    Declared capabilities, read with ``getattr`` by the ladder:
    ``level_rank = None`` (last-resort only, never tried while a preferred rung
    can deliver), ``last_resort = True`` (the same promise stated explicitly),
    and ``can_call = False`` (Discord cannot place a voice call).
    """

    name = CHANNEL_NAME
    level_rank = LEVEL_RANK
    last_resort = True
    can_call = False

    def __init__(
        self,
        config: Optional[DiscordConfig] = None,
        *,
        session: Any = None,
        http: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config = config if config is not None else DiscordConfig()
        self._http = http if http is not None else session

    # -- construction ------------------------------------------------------

    @classmethod
    def from_env_file(
        cls,
        path: Any = None,
        *,
        require_private_mode: bool = True,
        strict: bool = False,
        session: Any = None,
        http: Optional[Callable[..., Any]] = None,
    ) -> "DiscordTransport":
        config = load_config(
            path, require_private_mode=require_private_mode, strict=strict
        )
        return cls(config, session=session, http=http)

    def __repr__(self) -> str:
        return (
            f"<DiscordTransport name={self.name!r} level_rank={self.level_rank!r} "
            f"configured={self.available()} token={self.config.redacted_token} "
            f"target={self.config.target_description!r}>"
        )

    # -- availability ------------------------------------------------------

    def available(self) -> bool:
        """Whether a token and a recipient are configured.

        Answered from configuration only — no network call, no login probe. A
        probe would make availability depend on connectivity and would spend the
        very credential the caller is asking about.
        """
        return self.config.configured

    def blockers(self) -> Tuple[str, ...]:
        """Named, actionable reasons this transport is unavailable."""
        return self.config.blockers()

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Why the ladder skipped this rung, or ``None`` when it is available.

        Read with ``getattr`` by the ladder so a report can say "Discord: token
        missing from <path>" instead of the useless "unavailable".
        """
        problems = self.blockers()
        return " ".join(problems) if problems else None

    # -- HTTP --------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        # A user token goes in bare; the "Bot " prefix is for application bots
        # and Discord rejects it on these endpoints. This raw value is the one
        # place the token exists outside the config object.
        return {
            "Authorization": self.config.token,
            "Content-Type": "application/json",
            "User-Agent": self.config.user_agent,
        }

    def _poster(self) -> Any:
        if self._http is not None:
            return self._http
        try:
            import requests  # noqa: PLC0415 - lazy: only a real send needs it
        except ImportError as exc:  # pragma: no cover - requests is installed
            raise DiscordTransportError(
                f"[{CATEGORY_TRANSPORT_ERROR}] the 'requests' package is required to "
                f"actually send: install it with "
                f"'.venv/bin/python -m pip install requests'"
            ) from exc
        return requests.Session()

    def _post(self, url: str, payload: Mapping[str, Any]) -> Any:
        poster = self._poster()
        kwargs = {
            "json": dict(payload),
            "headers": self._headers(),
            "timeout": min(float(self.config.timeout), MAX_TIMEOUT_SECONDS),
        }
        try:
            if hasattr(poster, "post"):
                return poster.post(url, **kwargs)
            return poster(url, **kwargs)
        except DiscordError:
            raise
        except Exception as exc:
            # Never trust `str(exc)` verbatim: a requests error can quote the
            # request, headers included, so it is swept before it is quoted.
            detail = redact_text(str(exc), (self.config.token,))
            raise DiscordTransportError(
                f"[{CATEGORY_TRANSPORT_ERROR}] the Discord request did not complete "
                f"({exc.__class__.__name__}): {detail[:_BODY_EXCERPT_CHARS]}"
            ) from exc

    def _open_dm_channel(self) -> str:
        response = self._post(
            f"{self.config.api_base}/users/@me/channels",
            {"recipient_id": self.config.target_user_id},
        )
        status = _status_of(response)
        if not (200 <= status < 300):
            raise _status_error(status, response, self.config.token)
        body = _json_body(response)
        channel_id = str(body.get("id")) if body and body.get("id") else ""
        if not channel_id:
            raise DiscordApiError(
                status,
                f"[{CATEGORY_API_ERROR}] Discord answered HTTP {status} to the DM "
                f"open call but returned no channel id, so there is nowhere to post. "
                f"Body: {_body_excerpt(response, (self.config.token,)) or '<empty>'}",
            )
        return channel_id

    # -- send --------------------------------------------------------------

    def send(self, message: str, *, level: str, dry_run: bool = True) -> SendResult:
        """Post one message, or rehearse one.

        ``dry_run`` defaults to True so that a careless caller rehearses instead
        of DMing the user. A dry run makes **no** network call, not even the DM
        lookup. Nothing here ever reports delivery it did not achieve.
        """
        if not isinstance(message, str) or not message.strip():
            return self._failed(
                error=(
                    f"[{CATEGORY_INVALID_REQUEST}] refusing to send an empty message: "
                    f"give the transport non-empty text."
                ),
                detail=f"level={level!r}; the message was empty, so nothing was "
                f"rehearsed and nothing was sent",
            )
        if len(message) > MAX_MESSAGE_CHARS:
            return self._failed(
                error=(
                    f"[{CATEGORY_INVALID_REQUEST}] message is {len(message)} characters "
                    f"but Discord accepts at most {MAX_MESSAGE_CHARS}; split it into "
                    f"several messages."
                ),
                detail=f"level={level!r}; message too long ({len(message)} chars), "
                f"nothing was sent",
            )
        if not isinstance(level, str) or not level.strip():
            return self._failed(
                error=(
                    f"[{CATEGORY_INVALID_REQUEST}] the 'level' argument must name the "
                    f"report's urgency (low, medium, high, critical); got "
                    f"{level!r}."
                ),
                detail="level was not a usable urgency name, so nothing was sent",
            )

        problems = self.blockers()
        if problems:
            # A failure, not a rehearsal, in both modes: an unconfigured
            # transport cannot honestly claim that the message would have gone
            # out. `failed_result` records dry_run=False — no attempt was made
            # that could be called a rehearsal.
            return self._failed(
                error=" ".join(problems),
                detail=(
                    f"level={level!r}; not configured, so no network call was made, "
                    f"nothing was rehearsed and nothing was sent "
                    f"({self.config.target_description})"
                ),
            )

        # An operator who set DISCORD_TRANSPORT=dryrun means "never leave the
        # machine", whatever the caller asked for.
        forced_dry = self.config.mode_kind == MODE_DRY and not dry_run
        if dry_run or forced_dry:
            reason = (
                f"{TRANSPORT_KEY}=dryrun forces rehearsal"
                if forced_dry
                else "dry_run=True"
            )
            logger.info(
                "Discord transport rehearsing a %s-urgency report to %s; token %s; "
                "no network call (%s)",
                level,
                self.config.target_description,
                self.config.redacted_token,
                reason,
            )
            return rehearsed_result(
                CHANNEL_NAME,
                ACTION_SEND,
                (
                    f"rehearsed: would post one message to Discord "
                    f"{self.config.target_description} (level={level}); no network "
                    f"call was made ({reason}); token {self.config.redacted_token}"
                ),
            )

        return self._send_for_real(message, level)

    def _send_for_real(self, message: str, level: str) -> SendResult:
        logger.info(
            "Discord transport sending a %s-urgency report to %s; token %s",
            level,
            self.config.target_description,
            self.config.redacted_token,
        )
        try:
            if self.config.target_channel_id:
                channel_id = self.config.target_channel_id
            else:
                channel_id = self._open_dm_channel()
            response = self._post(
                f"{self.config.api_base}/channels/{channel_id}/messages",
                {"content": message},
            )
        except DiscordError as exc:
            return self._failed(
                error=str(exc),
                detail=(
                    f"level={level}; no message was delivered to "
                    f"{self.config.target_description}; token "
                    f"{self.config.redacted_token}"
                ),
            )

        status = _status_of(response)
        if not (200 <= status < 300):
            return self._failed(
                error=_status_error_message(status, response, self.config.token),
                detail=(
                    f"level={level}; no message was delivered to "
                    f"{self.config.target_description}; token "
                    f"{self.config.redacted_token}"
                ),
            )

        body = _json_body(response)
        message_id = str(body.get("id")) if body and body.get("id") else None
        if message_id is None:
            detail = (
                f"level={level}; Discord accepted the message (HTTP {status}) but the "
                f"response carried no message id, so delivery cannot be re-verified"
            )
        else:
            detail = (
                f"level={level}; posted one message to {self.config.target_description} "
                f"(HTTP {status}, message id {message_id}); token "
                f"{self.config.redacted_token}"
            )
        logger.info(
            "Discord transport delivered a %s-urgency report to %s (HTTP %s, message "
            "id %s)",
            level,
            self.config.target_description,
            status,
            message_id or "<none>",
        )
        return sent_result(
            CHANNEL_NAME, ACTION_SEND, detail, message_id=message_id
        )

    def _failed(self, *, error: str, detail: str) -> SendResult:
        """A failure: a real attempt (or a refusal) with a NAMED reason."""
        return failed_result(CHANNEL_NAME, error, ACTION_SEND, detail)


def _status_error(status: int, response: Any, token: str) -> DiscordError:
    """The right named error for a non-2xx status of the DM open call."""
    message = _status_error_message(status, response, token)
    if status in (401, 403):
        return DiscordCredentialRejectedError(status, message)
    if status == 429:
        return DiscordRateLimitedError(
            status, message, retry_after=_retry_after(response)
        )
    if 500 <= status < 600:
        return DiscordServerError(status, message)
    return DiscordApiError(status, message)


def _reason_phrase(status: int) -> str:
    # A tiny map instead of http.HTTPStatus slicing, to stay readable in errors.
    return {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        429: "Too Many Requests",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
        504: "Gateway Timeout",
    }.get(status, "Unknown")


def _status_error_message(status: int, response: Any, token: str) -> str:
    """A category-tagged, secret-free sentence naming the exact status.

    The three families the ladder treats differently are kept apart on purpose:
    401/403 means do not retry with this credential, 429 means back off and
    retry, 5xx means Discord's side broke and a later retry is reasonable.
    """
    phrase = _reason_phrase(status)
    excerpt = _body_excerpt(response, (token,))
    tail = f" Body: {excerpt}" if excerpt else ""

    if status in (401, 403):
        if status == 401:
            what = (
                "the credential was rejected: the selfbot token is invalid, expired "
                "or revoked"
            )
        else:
            what = (
                "the credential is not permitted to post here: the target blocks DMs, "
                "is not accepting messages, or the account is locked out"
            )
        return (
            f"[{CATEGORY_CREDENTIAL_REJECTED}] HTTP {status} {phrase} — {what}. "
            f"Re-issue the token and update {TOKEN_KEY} in "
            f"{CHANNELS_ENV_PATH} (chmod 600); retrying with the same token will "
            f"fail identically.{tail}"
        )
    if status == 429:
        retry_after = _retry_after(response)
        wait = f" Discord asked for a {retry_after}s wait." if retry_after else ""
        return (
            f"[{CATEGORY_RATE_LIMITED}] HTTP {status} {phrase} — the credential is "
            f"fine but Discord is throttling this transport; back off and retry "
            f"later.{wait}{tail}"
        )
    if 500 <= status < 600:
        return (
            f"[{CATEGORY_SERVER_ERROR}] HTTP {status} {phrase} — Discord failed on its "
            f"own side: nothing about the credential or the message is wrong, retry "
            f"later.{tail}"
        )
    return (
        f"[{CATEGORY_API_ERROR}] HTTP {status} {phrase} — Discord refused the "
        f"request; the message or the target is at fault, retrying unchanged will "
        f"fail the same way.{tail}"
    )
