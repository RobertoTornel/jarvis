"""Claire's outreach package: reaching the owner at the right level of urgency.

The ladder decides *whether* and *how* to reach the owner; transports know *how*
to talk to one channel. Both are separate so a channel can be written (and
tested) without the ladder, and the ladder can be tested with fakes and no
network.

Public names
------------
Contract:
    :class:`~claire.outreach.transport.Transport`,
    :class:`~claire.outreach.transport.SendResult`,
    and the honest constructors
    :func:`~claire.outreach.transport.sent_result`,
    :func:`~claire.outreach.transport.rehearsed_result`,
    :func:`~claire.outreach.transport.failed_result`.

Ladder:
    :func:`~claire.outreach.ladder.classify_urgency`,
    :class:`~claire.outreach.ladder.Ladder`,
    :class:`~claire.outreach.ladder.LadderResult`,
    :func:`~claire.outreach.ladder.route_for`,
    :func:`~claire.outreach.ladder.order_transports`,
    and the one-shot :func:`~claire.outreach.ladder.dispatch`.

Transport implementations live in sibling modules (e.g. a Signal transport, a
Discord transport). They are injected into the ladder by the caller; this package
never imports them, so it keeps working before they exist and never touches the
network on import. ``dry_run`` is the default everywhere: nothing leaves the
machine unless the caller explicitly passes ``dry_run=False``.
"""

from __future__ import annotations

from claire.outreach.ladder import (
    ACTION_NOTIFY,
    ACTION_NOTIFY_AND_CALL,
    ACTION_STAY,
    LEVELS,
    Ladder,
    LadderResult,
    Route,
    can_place_call,
    classify_urgency,
    dispatch,
    escalate,
    is_last_resort,
    order_transports,
    route_for,
    run,
)
from claire.outreach.transport import (
    ACTION_CALL,
    ACTION_SEND,
    OUTCOMES,
    SendResult,
    Transport,
    failed_result,
    rehearsed_result,
    sent_result,
)

__all__ = [
    "ACTION_CALL",
    "ACTION_NOTIFY",
    "ACTION_NOTIFY_AND_CALL",
    "ACTION_SEND",
    "ACTION_STAY",
    "LEVELS",
    "OUTCOMES",
    "Ladder",
    "LadderResult",
    "Route",
    "SendResult",
    "Transport",
    "can_place_call",
    "classify_urgency",
    "dispatch",
    "escalate",
    "failed_result",
    "is_last_resort",
    "order_transports",
    "rehearsed_result",
    "route_for",
    "run",
    "sent_result",
]
