"""Where a turn's seconds went: one honest journal line per answered turn.

The owner's number one complaint is that Claire's replies are slow, and until
now the journal said only *what* was heard (``📝 Heard: "…"``) — never how long
the turn took or where the wait went, so nobody could say where a turn's
seconds actually go on his machine. This module is the formatter for the one
line that answers that, and nothing else.

It is deliberately pure: no clocks, no I/O, no logging, no control flow. The
listener measures each span where the event actually happens and passes the
numbers in, which is what lets ``tests/test_turn_latency.py`` assert on real
strings instead of on timing luck.

Two rules the formatting keeps:

* **Never invent a number.** A field that was not measured for this turn — no
  voice moment, a decode that never ran, a superseded turn — renders as ``-``,
  never as ``0.0``. A zero reads like a measurement; a dash reads like what it
  is: nothing was measured.
* **The total is its own measurement**: the span from the owner's last word to
  the reply being ready, passed in and never recomputed from the parts. The
  decode normally finishes *inside* the silence window and the reply engine
  can wait on the shared query lock, so a summed total would be a fiction.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Optional

#: Rendered in place of a duration that was never measured. Deliberately
#: distinct from ``0.0s``: zero would claim a measurement that does not exist.
MISSING = "-"


def _duration(value: Optional[float]) -> str:
    """Format one measured duration as ``<seconds>.1f s``, or ``-``.

    ``None`` means "not measured for this turn". A negative or non-finite
    value is a clock artifact rather than a duration, so it is reported the
    same honest way instead of being printed as if it were real.
    """
    if value is None:
        return MISSING
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return MISSING
    if not isfinite(seconds) or seconds < 0.0:
        return MISSING
    return f"{seconds:.1f}s"


def format_turn_line(
    *,
    silence: Optional[float],
    stt: Optional[float],
    reply: Optional[float],
    total: Optional[float],
    query: str,
) -> str:
    """One journal line: the four measured numbers plus the utterance.

    ``silence`` is the wait after the owner's last word until the turn was
    dispatched, ``stt`` the decode of the turn's final transcript, ``reply``
    how long the reply engine took, and ``total`` the measured span from last
    word to reply ready. Each renders as ``<n>.1f s`` or ``-`` when it was
    never measured; the query is collapsed so the journal stays one line.
    """
    text = " ".join(str(query or "").split()).replace('"', "'")
    return (
        f"⏱️ turn {_duration(total)}: "
        f"silence {_duration(silence)} + stt {_duration(stt)} + "
        f'reply {_duration(reply)} — "{text}"'
    )


def measured_stt_seconds(turn: Any) -> Optional[float]:
    """This turn's authoritative decode duration, or ``None`` if there is none.

    The duration comes from the turn's own recorded decode timestamps — the
    authoritative recogniser's start/finish, already measured where the
    transcription ran. ``None`` (rendered ``-``) means no authoritative decode
    was measured *for this turn*: there is no turn, the turn is degraded (its
    text was promoted from the streaming/provisional recogniser because the
    authoritative engine never ran), or the turn carries no authoritative
    timings. A provisional or cached decode's duration is not this turn's
    decode duration, and printing one anyway would be a plausible-looking
    wrong number.
    """
    if turn is None or getattr(turn, "degraded", False):
        return None
    try:
        duration = turn.timings().get("authoritative_sec")
        if duration is None:
            return None
        seconds = float(duration)
    except Exception:
        # Anything unexpected reads as "not measured": a dash, not a number
        # this module cannot vouch for.
        return None
    if not isfinite(seconds) or seconds < 0.0:
        return None
    return seconds
