"""The transport contract for Claire's outreach ladder.

This module is the *shared interface* every outreach transport implements:
Signal, Discord, voice call, and any future channel. It is deliberately tiny so
independently-written transports compose, and deliberately strict so a transport
cannot lie about what it did.

Three outcomes, never collapsed
-------------------------------
A transport must be able to say honestly which of exactly three things happened:

* **sent** -- a real message/call left the machine
  (``delivered=True``, ``dry_run=False``, ``error is None``).
* **rehearsed** -- nothing left the machine because the caller asked for a dry
  run (``delivered=False``, ``dry_run=True``, ``error is None``). A dry run must
  not make a network call; the transport reports what *would* have been sent.
* **failed** -- the attempt was made and did not succeed, with a NAMED reason
  (``delivered=False``, ``dry_run=False``, ``error="<named reason>"``).

``SendResult.__post_init__`` rejects any combination that would blur those three
(notably ``delivered=False, dry_run=False, error=None`` -- the silent no-op, and
``delivered=True, dry_run=True`` -- a rehearsal claiming delivery). Transports
should build results with :func:`sent_result`, :func:`rehearsed_result` and
:func:`failed_result` rather than hand-writing the flags.

Capabilities a transport may declare (all optional, read with ``getattr`` by
:mod:`claire.outreach.ladder`):

``level_rank``
    Required by the :class:`Transport` protocol. An ``int`` where smaller is
    preferred (Signal 1, a secondary text channel 2, ...). ``None`` means the
    transport is **not an ordinary rung**: it is last-resort only. Discord uses
    ``None``.
``last_resort``
    Optional explicit ``bool``; ``True`` pins the transport after every
    preferred transport regardless of its rank. The ladder also recognises a
    transport by name (``discord``/``discord_*``) as last resort even if a
    transport forgets to declare it, because "Discord is never first" is a
    guarantee, not a convention.
``can_call``
    Optional ``bool`` or zero-argument callable; ``True`` means the transport can
    place a real voice call (only Signal can). The ladder also treats names such
    as ``signal_call``/``voice_call``/``*_call`` as call-capable, and never treats
    a Discord-named transport as call-capable.
``unavailable_reason``
    Optional ``str`` set when :meth:`Transport.available` returns ``False``, so
    the ladder can report *why* a rung was skipped instead of "unavailable".
``call(message, *, level, dry_run)``
    Optional alternative to ``send`` for the voice-call step. A transport that
    both texts and calls implements ``call`` so the ladder does not have to guess
    which action was requested; its result carries ``action="call"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

__all__ = [
    "ACTION_CALL",
    "ACTION_SEND",
    "OUTCOMES",
    "SendResult",
    "Transport",
    "failed_result",
    "rehearsed_result",
    "sent_result",
]

#: Result/action names. ``action`` says what the transport did, not how urgent
#: the message was.
ACTION_SEND = "send"
ACTION_CALL = "call"

#: The three honest outcomes, in ladder/UI presentation order.
OUTCOMES = ("sent", "rehearsed", "failed")


@dataclass
class SendResult:
    """What one transport attempt actually did.

    Fields are exactly the shared interface: ``action``, ``channel``,
    ``delivered``, ``dry_run``, ``detail``, then optional ``message_id`` and
    ``error``. Extra behaviour (``outcome``, ``ok``) is read-only and additive,
    so positionally-constructed results from sibling transports keep working.
    """

    action: str
    channel: str
    delivered: bool
    dry_run: bool
    detail: str
    message_id: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.delivered and self.dry_run:
            raise ValueError(
                "SendResult is contradictory: delivered=True with dry_run=True "
                "(a dry run cannot deliver). Report the rehearsal with "
                "delivered=False, dry_run=True, or the real send with "
                "delivered=True, dry_run=False."
            )
        if self.delivered and self.error is not None:
            raise ValueError(
                "SendResult is contradictory: delivered=True with error set "
                f"({self.error!r}). A delivered message has no error; a failed "
                "attempt must report delivered=False, dry_run=False and the "
                "named error."
            )
        if not self.delivered and not self.dry_run and self.error is None:
            raise ValueError(
                f"SendResult from channel {self.channel!r} would be a silent "
                "no-op: delivered=False, dry_run=False and no error. A real "
                "attempt must either deliver or name its failure -- pass "
                "error='<named reason>' (and ideally also patch the transport "
                "so it reports credentials/dependencies it is missing)."
            )

    # -- the three outcomes ------------------------------------------------

    @property
    def outcome(self) -> str:
        """``"sent"``, ``"rehearsed"`` or ``"failed"`` -- never anything else."""
        if self.delivered:
            return "sent"
        if self.dry_run:
            return "rehearsed"
        return "failed"

    @property
    def ok(self) -> bool:
        """True when the attempt did not fail.

        A rehearsal is ``ok`` for ladder purposes (the transport is available
        and the plan holds), which is why a dry run never escalates to the next
        rung. A failure is ``not ok`` and the ladder falls through.
        """
        return self.error is None and (self.delivered or self.dry_run)

    @property
    def is_failure(self) -> bool:
        return not self.ok

    def summary(self) -> str:
        """One-line, secret-free description for logs and results."""
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
    """A message or call that really left the machine."""
    return SendResult(
        action=action,
        channel=channel,
        delivered=True,
        dry_run=False,
        detail=detail or "sent",
        message_id=message_id,
    )


def rehearsed_result(
    channel: str,
    action: str = ACTION_SEND,
    detail: str = "",
) -> SendResult:
    """A dry run: nothing left the machine, no network call was made."""
    return SendResult(
        action=action,
        channel=channel,
        delivered=False,
        dry_run=True,
        detail=detail or "dry run: nothing was sent",
    )


def failed_result(
    channel: str,
    reason: str,
    action: str = ACTION_SEND,
    detail: str = "",
) -> SendResult:
    """A real attempt that failed, with a NAMED reason (never a silent no-op)."""
    if not reason or not str(reason).strip():
        raise ValueError(
            "failed_result requires a named reason: pass the missing credential, "
            "dependency or transport error, e.g. "
            "failed_result('signal', 'Signal CLI not found: install signal-cli "
            "or set signal_cli_path')"
        )
    return SendResult(
        action=action,
        channel=channel,
        delivered=False,
        dry_run=False,
        detail=detail or f"failed: {reason}",
        error=str(reason),
    )


@runtime_checkable
class Transport(Protocol):
    """A channel the ladder can try.

    ``name`` is the stable channel id (``"signal"``, ``"discord"``,
    ``"signal_call"``); ``level_rank`` orders ordinary rungs (smaller first) and
    ``None`` marks a last-resort-only transport. ``available()`` must be cheap and
    must not make a network call -- it reports whether the transport is
    configured and usable right now. ``send`` must honour ``dry_run``: with
    ``dry_run=True`` it rehearses and makes no network call.
    """

    name: str
    level_rank: Optional[int]

    def available(self) -> bool:
        """True when this transport is configured and usable right now."""
        ...

    def send(
        self, message: str, *, level: str, dry_run: bool = True
    ) -> SendResult:
        """Deliver (or rehearse) ``message``; never raise for a failed send."""
        ...
