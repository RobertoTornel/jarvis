"""Claire's urgency ladder: how a message reaches the owner.

Mirrors how the owner's own assistant reaches *them*: most things stay in the
conversation, some deserve a text, a few deserve a second attempt, and an
emergency deserves a text **and a phone call**. Discord is the last resort and is
never tried while a preferred transport can work.

The ladder is offline by default
--------------------------------
``dry_run`` is the default everywhere. In a dry run no network call is made and
nothing leaves the machine: each transport *rehearses* the exact message text and
reports ``delivered=False, dry_run=True``. Live sending happens only when the
caller explicitly passes ``dry_run=False``.

Routing
-------
===========  ===========================================================
level        what the ladder does
===========  ===========================================================
``low``      nothing is sent; this stays in the conversation. That is a
             normal, successful outcome, explicitly reported
             (``sent_anything=False``, ``nothing_sent_reason`` set) -- it is
             not a silent no-op.
``medium``   one text through the first preferred transport that works.
``high``     a text, retried once on the same transport if the first
             attempt fails, then falling through to the next transport.
``critical`` a text (as ``high``) and then a **voice call**. Only Signal can
             place a call; Discord cannot. The result always says plainly
             whether a call was actually placed (``called``) and, when it was
             not, names why (``call_reason``).
===========  ===========================================================

Ordering, fall-through and honesty
----------------------------------
Transports are injected, so tests use fakes and no network is touched. They are
tried in order: ordinary rungs (``level_rank`` ascending) first, then
last-resort transports. Discord is last resort by construction -- either the
transport declares ``last_resort = True`` / ``level_rank = None``, or its name is
``discord``/``discord_*`` (the ladder enforces that name rule even when a
transport forgets to declare it, because "Discord is never first" is a guarantee).

A transport that is unavailable, that raises, or that returns a failure never
aborts the ladder: the attempt is recorded, the ladder falls through to the next
transport, and the whole attempt is reported in :class:`LadderResult` -- which
transports were tried, what each did (``sent`` / ``rehearsed`` / ``failed``), which
were skipped and why. A rehearsal counts as success for ordering purposes, so a
dry run never escalates into reaching a channel the live run would not use.

Transport errors and availability reasons are passed through
:mod:`claire.utils.redact` before they appear in a result, so a failure message
carrying a token cannot leak it into a log or a reply. The ladder itself never
logs or echoes the message: in a dry run it hands the exact text back as
``LadderResult.rehearsal`` -- that is the whole point of a rehearsal -- and in
live mode it echoes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional, Sequence

from claire.outreach.transport import (
    ACTION_CALL,
    ACTION_SEND,
    SendResult,
    Transport,
    failed_result,
)
from claire.utils.redact import redact, scrub_secrets

__all__ = [
    "ACTION_NOTIFY",
    "ACTION_NOTIFY_AND_CALL",
    "ACTION_STAY",
    "LEVELS",
    "Ladder",
    "LadderResult",
    "Route",
    "Transport",
    "can_place_call",
    "classify_urgency",
    "dispatch",
    "escalate",
    "is_last_resort",
    "order_transports",
    "route_for",
    "run",
]

#: Urgency levels, lowest first. An explicit level is always one of these.
LEVELS = ("low", "medium", "high", "critical")

#: Headline action for the outcome, independent of channel.
ACTION_STAY = "stay_in_conversation"
ACTION_NOTIFY = "notify"
ACTION_NOTIFY_AND_CALL = "notify_and_call"

_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# --- wording lexicons -------------------------------------------------------
# Scanned in this order: an emergency word outranks "important", which outranks
# "let me know". An explicit override bypasses the scan entirely.

_CRITICAL_PHRASES = (
    "emergency",
    "life-threatening",
    "life threatening",
    "can't breathe",
    "cannot breathe",
    "not breathing",
    "chest pain",
    "heart attack",
    "stroke",
    "severe bleeding",
    "bleeding badly",
    "overdose",
    "unconscious",
    "ambulance",
    "call 911",
    "911",
    "fire",
    "intruder",
    "burglar",
    "danger",
    "critical",
    "code red",
    "sos",
    "help me now",
    "i need help now",
    "medical emergency",
)

_HIGH_PHRASES = (
    "urgent",
    "urgency",
    "urgently",
    "immediately",
    "asap",
    "as soon as possible",
    "important",
    "time-sensitive",
    "time sensitive",
    "deadline",
    "right away",
    "do not ignore",
    "don't ignore",
    "needs attention now",
    "please respond",
)

_MEDIUM_PHRASES = (
    "soon",
    "today",
    "this afternoon",
    "this evening",
    "tonight",
    "let me know",
    "notify me",
    "notify",
    "update me",
    "heads up",
    "reminder",
    "remember to",
    "message me",
    "ping me",
    "text me",
)

_LOW_PHRASES = (
    "fyi",
    "no rush",
    "no hurry",
    "whenever",
    "when you get a chance",
    "just so you know",
    "no pressure",
    "not urgent",
    "not important",
)

#: Negations are stripped before the scan, so "not urgent" does not classify as
#: high. Phrases left over after stripping can still raise the level (e.g.
#: "no rush, but the deadline is today" is medium via "deadline").
_NEGATED_PHRASES = (
    "not urgent",
    "no rush",
    "no hurry",
    "no need to rush",
    "not important",
    "no urgency",
    "no pressure",
    "don't rush",
    "dont rush",
    "no need to hurry",
)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    # Word-ish boundaries that survive apostrophes ("can't") and hyphenated
    # compounds ("life-threatening").
    return re.compile(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])")


_KEYWORD_PATTERNS: list[tuple[str, tuple[re.Pattern[str], ...]]] = [
    (level, tuple(_phrase_pattern(p) for p in phrases))
    for level, phrases in (
        ("critical", _CRITICAL_PHRASES),
        ("high", _HIGH_PHRASES),
        ("medium", _MEDIUM_PHRASES),
        ("low", _LOW_PHRASES),
    )
]


def classify_urgency(text: Optional[str], *, override: Optional[str] = None) -> str:
    """Classify ``text`` as ``low`` | ``medium`` | ``high`` | ``critical``.

    An explicit ``override`` ALWAYS wins over the keywords -- including when the
    wording says something else ("low, but the house is on fire" stays low when
    the caller says low). An override that is not a known level raises a named
    :class:`ValueError` rather than silently falling back to keywords.

    ``None`` is treated as empty text and classifies as ``low``. Text with no
    matching wording is ``low``: by default nothing is sent.
    """
    if override is not None:
        candidate = str(override).strip().lower()
        if candidate not in LEVELS:
            raise ValueError(
                f"unknown urgency level {override!r}: explicit override must be "
                f"one of {', '.join(LEVELS)}. Pass a valid level, or omit the "
                "override to classify from the wording."
            )
        return candidate

    if text is None:
        text = ""
    if not isinstance(text, str):
        raise TypeError(
            f"classify_urgency expects text as str or None, got "
            f"{type(text).__name__}: pass the message text, or an explicit "
            "override= level."
        )

    normalized = " " + " ".join(text.lower().split()) + " "
    for phrase in _NEGATED_PHRASES:
        normalized = normalized.replace(phrase, " ")

    for level, patterns in _KEYWORD_PATTERNS:
        if any(pattern.search(normalized) for pattern in patterns):
            return level
    return "low"


@dataclass(frozen=True)
class Route:
    """What a level means, so routing is inspectable without running it."""

    level: str
    action: str
    send: bool
    retries: int
    call: bool

    @property
    def max_attempts(self) -> int:
        """Attempts allowed per transport for the text step."""
        return self.retries + 1 if self.send else 0


ROUTES: dict[str, Route] = {
    "low": Route("low", ACTION_STAY, send=False, retries=0, call=False),
    "medium": Route("medium", ACTION_NOTIFY, send=True, retries=0, call=False),
    "high": Route("high", ACTION_NOTIFY, send=True, retries=1, call=False),
    "critical": Route("critical", ACTION_NOTIFY_AND_CALL, send=True, retries=1, call=True),
}


def route_for(level: str) -> Route:
    """The :class:`Route` for ``level``; a named error for anything else."""
    candidate = str(level).strip().lower()
    if candidate not in ROUTES:
        raise ValueError(
            f"unknown urgency level {level!r}: expected one of "
            f"{', '.join(LEVELS)}"
        )
    return ROUTES[candidate]


# --- transport policy -------------------------------------------------------


def _transport_name(transport: object) -> str:
    name = getattr(transport, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return type(transport).__name__


def _is_discord_name(name: str) -> bool:
    lowered = name.lower()
    return lowered == "discord" or lowered.startswith("discord")


def _failure_text(attempt: SendResult) -> str:
    """``channel: reason`` without repeating a channel the transport already named."""
    error = attempt.error or "failed without naming a reason"
    if attempt.channel and not error.startswith(f"{attempt.channel}:"):
        return f"{attempt.channel}: {error}"
    return error


def _scrub(result: SendResult) -> SendResult:
    """Mask secrets in text a transport handed us, keeping the outcome intact.

    The ladder is a boundary: a transport's failure text can mention a URL with a
    bot token or an expired credential. The flags (``delivered``/``dry_run``/
    ``error``) are preserved exactly -- only obvious secret shapes are masked --
    so the three outcomes stay distinguishable and nothing secret is reported.
    """
    detail = scrub_secrets(result.detail if isinstance(result.detail, str) else str(result.detail))
    error = (
        scrub_secrets(result.error if isinstance(result.error, str) else str(result.error))
        if result.error is not None
        else None
    )
    if detail == result.detail and error == result.error:
        return result
    return replace(result, detail=detail, error=error)


def is_last_resort(transport: object) -> bool:
    """True when ``transport`` must only be tried after every preferred one.

    Discord is recognised by name first, so a misconfigured Discord transport
    can never be tried first. Otherwise ``last_resort = True`` or a missing
    ``level_rank`` marks a last-resort-only transport.
    """
    if _is_discord_name(_transport_name(transport)):
        return True
    explicit = getattr(transport, "last_resort", None)
    if explicit is not None:
        return bool(explicit)
    return getattr(transport, "level_rank", None) is None


def order_transports(transports: Iterable[object]) -> list[object]:
    """Preferred transports first (``level_rank`` ascending), last resort last.

    A non-transport object is kept (and will be reported as unavailable by the
    ladder) rather than silently dropped.
    """
    preferred: list[tuple[tuple[int, int, int], object]] = []
    last: list[tuple[tuple[int, int, int], object]] = []
    for index, transport in enumerate(transports):
        rank = getattr(transport, "level_rank", None)
        has_rank = isinstance(rank, int) and not isinstance(rank, bool)
        key = (0 if has_rank else 1, rank if has_rank else 0, index)
        (last if is_last_resort(transport) else preferred).append((key, transport))
    preferred.sort(key=lambda item: item[0])
    last.sort(key=lambda item: item[0])
    return [transport for _, transport in preferred + last]


def can_place_call(transport: object) -> bool:
    """True when ``transport`` can place a real voice call.

    Discord never can. A transport declares the capability with ``can_call``
    (bool or callable), ``place_call``/``call`` (callable), ``supports_call`` or
    ``supports_calls``, or an unmistakable name (``signal_call``, ``voice_call``,
    anything ending in ``_call``).
    """
    name = _transport_name(transport)
    if _is_discord_name(name):
        return False
    if callable(getattr(transport, "call", None)):
        # An explicit call() entry point is the strongest statement of intent.
        return True
    if callable(getattr(transport, "place_call", None)):
        # ``place_call`` is the method name the shared interface documents, so a
        # transport implementing only that must still be recognised. Without this,
        # a future transport could silently never be dialled on a critical alert -
        # the one thing only Signal can do.
        return True
    supports = getattr(transport, "supports_calls", None)
    if callable(supports):
        try:
            if bool(supports()):
                return True
        except Exception:
            pass
    elif supports is True:
        return True
    declared = getattr(transport, "can_call", None)
    if callable(declared):
        try:
            return bool(declared())
        except Exception:
            return False
    if isinstance(declared, bool):
        return declared
    if getattr(transport, "supports_call", None) is True:
        return True
    lowered = name.lower().replace("-", "_")
    return lowered in {"signal_call", "voice_call", "call"} or lowered.endswith("_call")


# --- results ----------------------------------------------------------------


@dataclass
class LadderResult:
    """An honest account of one trip up (or deliberately not up) the ladder."""

    level: str
    action: str
    dry_run: bool
    sent_anything: bool
    delivered: bool
    called: bool
    call_attempted: bool
    attempts: list[SendResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    channel: Optional[str] = None
    call_reason: Optional[str] = None
    nothing_sent_reason: Optional[str] = None
    error: Optional[str] = None
    detail: str = ""
    #: In a dry run, the exact text that WOULD have been sent, so the owner can
    #: read it. ``None`` in live mode: the ladder never echoes a live message.
    rehearsal: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when the ladder did what this level required of it."""
        if self.level == "low":
            return True
        text_ok = any(a.ok and a.action != ACTION_CALL for a in self.attempts)
        return text_ok or (self.level == "critical" and self.called)

    @property
    def call_placed(self) -> bool:
        """Alias of ``called``: a real voice call was placed."""
        return self.called

    @property
    def outcomes(self) -> list[str]:
        """``"sent"`` / ``"rehearsed"`` / ``"failed"`` for each attempt."""
        return [a.outcome for a in self.attempts]

    @property
    def tried(self) -> list[str]:
        """Channel names that were actually attempted, in order."""
        return [a.channel for a in self.attempts]

    def summary(self) -> str:
        return self.detail


class Ladder:
    """Routes a message by urgency through injected transports.

    ``dry_run`` is the instance default and stays True unless the caller passes
    ``dry_run=False`` explicitly to :meth:`run`.
    """

    def __init__(self, transports: Iterable[object], *, dry_run: bool = True) -> None:
        if transports is None:
            transports = []
        if isinstance(transports, (str, bytes)) or not isinstance(transports, Iterable):
            raise TypeError(
                "transports must be an iterable of transport objects, got "
                f"{type(transports).__name__}: pass e.g. Ladder([signal, discord])"
            )
        if not isinstance(dry_run, bool):
            raise TypeError(
                "dry_run must be a bool, got "
                f"{type(dry_run).__name__}: pass dry_run=False to send for real, "
                "or omit it to rehearse (the safe default)."
            )
        self.transports = list(transports)
        self.dry_run_default = dry_run

    def classify(self, text: Optional[str], *, level: Optional[str] = None) -> str:
        return classify_urgency(text, override=level)

    def run(
        self,
        message: str,
        *,
        level: Optional[str] = None,
        dry_run: Optional[bool] = None,
    ) -> LadderResult:
        """Classify (or accept an explicit ``level``) and route ``message``.

        ``message`` is sent verbatim: a dry run rehearses the exact text, and the
        ladder never rewrites it. Returns a :class:`LadderResult`; a transport
        being unavailable, raising, or failing never raises out of here.
        """
        if level is not None and not isinstance(level, str):
            raise TypeError(
                f"level must be a str or None, got {type(level).__name__}: pass "
                f"one of {', '.join(LEVELS)}, or omit it to classify from the "
                "wording."
            )
        if dry_run is not None and not isinstance(dry_run, bool):
            raise TypeError(
                "dry_run must be a bool or None, got "
                f"{type(dry_run).__name__}: pass dry_run=False to send for real."
            )
        if not isinstance(message, str):
            raise TypeError(
                f"message must be a str, got {type(message).__name__}: pass the "
                "text to deliver (it is also what gets classified when no level "
                "is given)."
            )

        resolved = classify_urgency(message, override=level)
        # ``dry_run`` is the effective flag; ``live`` is its negation, kept only
        # for the human-readable report so "nothing left the machine" can never
        # be printed about a live send, or vice versa.
        dry_run = self.dry_run_default if dry_run is None else dry_run
        live = not dry_run
        route = route_for(resolved)
        ordered = order_transports(self.transports)

        if not route.send:
            # Nothing is sent, and that is reported, not hidden.
            reason = "urgency is low: this stays in the conversation"
            return LadderResult(
                level=resolved,
                action=route.action,
                dry_run=dry_run,
                sent_anything=False,
                delivered=False,
                called=False,
                call_attempted=False,
                nothing_sent_reason=reason,
                detail=(
                    f"{reason}; nothing was sent and no transport was touched "
                    f"({'live mode' if live else 'dry run'})"
                ),
            )

        attempts: list[SendResult] = []
        skipped: list[str] = []
        channel: Optional[str] = None

        for transport in ordered:
            name = _transport_name(transport)
            usable, why = self._availability(transport)
            if not usable:
                skipped.append(why)
                continue
            accepted = False
            for _ in range(route.max_attempts):
                attempt = self._attempt(
                    transport, message, resolved, dry_run=dry_run, action=ACTION_SEND
                )
                attempts.append(attempt)
                if attempt.ok:
                    accepted = True
                    channel = attempt.channel or name
                    break
            if accepted:
                break

        called = False
        call_attempted = False
        call_reason: Optional[str] = None
        if route.call:
            called, call_attempted, call_reason = self._call_step(
                ordered, message, dry_run, attempts, skipped
            )

        delivered = any(a.delivered for a in attempts)
        sent_anything = delivered
        error = self._failure_error(resolved, route, attempts, skipped)
        detail = self._describe(
            resolved, live, attempts, skipped, called, call_reason, error
        )

        return LadderResult(
            level=resolved,
            action=route.action,
            dry_run=dry_run,
            sent_anything=sent_anything,
            delivered=delivered,
            called=called,
            call_attempted=call_attempted,
            attempts=attempts,
            skipped=skipped,
            channel=channel or (attempts[-1].channel if attempts else None),
            call_reason=call_reason,
            error=error,
            detail=detail,
            rehearsal=message if dry_run else None,
        )

    # -- internals ---------------------------------------------------------

    def _availability(self, transport: object) -> tuple[bool, str]:
        name = _transport_name(transport)
        check = getattr(transport, "available", None)
        if not callable(check):
            return False, (
                f"{name}: not a usable transport -- it has no available() method; "
                "provide one implementing claire.outreach.transport.Transport"
            )
        try:
            usable = bool(check())
        except Exception as exc:  # a broken transport must not abort the ladder
            return False, scrub_secrets(
                f"{name}: availability check raised {type(exc).__name__}: {exc}"
            )
        if usable:
            return True, ""
        reason = getattr(transport, "unavailable_reason", None) or getattr(
            transport, "reason", None
        )
        if reason:
            return False, scrub_secrets(f"{name}: unavailable -- {reason}")
        return False, (
            f"{name}: unavailable (no reason reported -- give the transport a "
            "NAMED dependency/credential error and expose it as "
            "unavailable_reason)"
        )

    def _attempt(
        self,
        transport: object,
        message: str,
        level: str,
        *,
        dry_run: bool,
        action: str,
    ) -> SendResult:
        name = _transport_name(transport)
        if action == ACTION_CALL:
            sender = getattr(transport, "call", None)
            if not callable(sender):
                sender = getattr(transport, "send", None)
        else:
            sender = getattr(transport, "send", None)
        if not callable(sender):
            return failed_result(
                name,
                f"{name}: transport has no send() method -- implement "
                "claire.outreach.transport.Transport",
                action=action,
            )
        try:
            result = sender(message, level=level, dry_run=dry_run)
        except TypeError as exc:
            # Most likely the transport's signature does not accept the contract.
            return failed_result(
                name,
                scrub_secrets(
                    f"{name}: send() rejected the shared signature "
                    f"(message, *, level, dry_run): {type(exc).__name__}: {exc}"
                ),
                action=action,
            )
        except Exception as exc:  # never let a transport abort the ladder
            return failed_result(
                name,
                scrub_secrets(f"{name}: {action} raised {type(exc).__name__}: {exc}"),
                action=action,
            )
        if not isinstance(result, SendResult):
            return failed_result(
                name,
                f"{name}: {action} returned {type(result).__name__}, expected "
                "SendResult -- report sent/rehearsed/failed honestly",
                action=action,
            )
        return _scrub(result)

    def _call_step(
        self,
        ordered: Sequence[object],
        message: str,
        dry_run: bool,
        attempts: list[SendResult],
        skipped: list[str],
    ) -> tuple[bool, bool, Optional[str]]:
        """Place (or rehearse) the voice call. Returns (called, attempted, why)."""
        candidates = [t for t in ordered if can_place_call(t)]
        if not candidates:
            return (
                False,
                False,
                "no injected transport can place a voice call: only Signal can "
                "call (declare can_call = True), Discord cannot. Nothing was "
                "called.",
            )

        attempted = False
        reasons: list[str] = []
        for transport in candidates:
            name = _transport_name(transport)
            usable, why = self._availability(transport)
            if not usable:
                skipped.append(why)
                reasons.append(why)
                continue
            attempted = True
            attempt = self._attempt(
                transport, message, "critical", dry_run=dry_run, action=ACTION_CALL
            )
            attempts.append(attempt)
            if attempt.action != ACTION_CALL:
                reasons.append(
                    f"{name} answered the call step with action={attempt.action!r} "
                    "instead of a voice call: it cannot place a call (only Signal "
                    "can; Discord cannot). Nothing was called."
                )
                continue
            if attempt.delivered:
                return True, True, None
            if attempt.dry_run:
                return (
                    False,
                    True,
                    "dry run: the voice call was rehearsed, NOT placed -- nothing "
                    "left the machine",
                )
            reasons.append(attempt.error or f"{name}: the voice call failed.")

        if not attempted:
            return (
                False,
                False,
                "no call-capable transport was available: "
                + "; ".join(reasons)
                + ". Nothing was called.",
            )
        return (
            False,
            True,
            "no voice call was placed: " + "; ".join(reasons),
        )

    def _failure_error(
        self,
        level: str,
        route: Route,
        attempts: Sequence[SendResult],
        skipped: Sequence[str],
    ) -> Optional[str]:
        if not route.send:
            return None
        if any(a.ok for a in attempts if a.action != ACTION_CALL):
            return None
        if not attempts and not skipped:
            return (
                "no transports were injected: pass the transports to try, e.g. "
                "Ladder([signal_transport, discord_transport])"
            )
        failures = [_failure_text(a) for a in attempts if a.error]
        parts = failures + list(skipped)
        if not parts:
            return None
        return redact(
            f"nothing could be sent at level {level}: " + "; ".join(parts)
        )

    def _describe(
        self,
        level: str,
        live: bool,
        attempts: Sequence[SendResult],
        skipped: Sequence[str],
        called: bool,
        call_reason: Optional[str],
        error: Optional[str],
    ) -> str:
        parts: list[str] = []
        sent = [a for a in attempts if a.outcome == "sent"]
        rehearsed = [a for a in attempts if a.outcome == "rehearsed"]
        failed = [a for a in attempts if a.outcome == "failed"]

        if live:
            if sent:
                parts.append(
                    "sent for real via "
                    + ", ".join(f"{a.channel} ({a.action})" for a in sent)
                )
            else:
                parts.append("nothing was sent for real")
        else:
            parts.append("dry run: nothing left the machine")
            if rehearsed:
                parts.append(
                    "rehearsed via "
                    + ", ".join(f"{a.channel} ({a.action})" for a in rehearsed)
                )
        if failed:
            parts.append(
                "failed attempts: " + "; ".join(_failure_text(a) for a in failed)
            )
        if skipped:
            parts.append("skipped: " + "; ".join(skipped))
        if level == "critical":
            parts.append(
                "voice call placed" if called else f"no voice call placed ({call_reason})"
            )
        if error:
            parts.append(error)
        return redact(" | ".join(parts))


def dispatch(
    message: str,
    transports: Iterable[object],
    *,
    level: Optional[str] = None,
    dry_run: bool = True,
) -> LadderResult:
    """One-shot convenience: route ``message`` through ``transports``.

    ``dry_run`` defaults to True; pass ``dry_run=False`` to send for real.
    """
    return Ladder(transports, dry_run=dry_run).run(message, level=level)


#: Convenience aliases other modules may use.
run = dispatch
escalate = dispatch
