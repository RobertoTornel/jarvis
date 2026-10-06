"""One utterance, one authoritative transcript.

Decision D3 gives Claire two recognisers, which creates exactly one danger: the
text she *acts on* and the text she *remembers* can disagree. The reference
project deleted its second-pass recogniser over precisely this defect — its
120-second context buffer was built from streaming text while the turn itself
carried corrected text, so the same utterance had two versions.

:class:`TurnTranscript` is the defence. It carries both texts, and exposes them
through *named accessors with different audience*:

======================  ==========================================
accessor                who may use it
======================  ==========================================
``provisional_text``    wake detection and pre-wake context only
``authoritative_text``  intent, tools, reply, diary, knowledge graph
======================  ==========================================

Anything that feeds the model's memory, the reply or a tool call must go through
:meth:`TurnTranscript.authoritative_for`, which refuses provisional input. The
divergence suite (``tests/test_transcript_divergence.py``) feeds deliberately
contradictory transcripts and asserts that the tool call, the reply, the diary
row and the graph entry all used the authoritative text, and that no memory row
contains the provisional one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: Where an utterance's authoritative text came from. ``streaming_fallback``
#: means the authoritative engine did not run and the provisional text was
#: promoted — it is recorded honestly so a degraded turn is never mistaken for
#: a verified one.
SOURCE_AUTHORITATIVE_NEMO = "parakeet_nemo"
SOURCE_AUTHORITATIVE_ONNX = "parakeet_onnx"
SOURCE_STREAMING_FALLBACK = "streaming_fallback"


class ProvisionalTextRejected(ValueError):
    """Raised when provisional streaming text is about to be treated as truth.

    This is a programming error, not a runtime condition: it means a caller
    wired the streaming transcript into something that must only ever see the
    authoritative one.
    """


@dataclass(frozen=True)
class TurnTranscript:
    """Both transcripts of a single utterance, plus the timings to judge them."""

    provisional_text: str
    authoritative_text: str
    source: str
    # Monotonic timestamps (time.monotonic()), so they are only meaningful
    # relative to each other.
    provisional_started_at: float | None = None
    provisional_finalized_at: float | None = None
    authoritative_started_at: float | None = None
    authoritative_finalized_at: float | None = None
    # Set when the turn was promoted from the streaming transcript because no
    # authoritative engine was available. Never silent: the caller must be able
    # to tell the user the transcript was provisional.
    degraded_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- truth -------------------------------------------------------------

    @property
    def degraded(self) -> bool:
        """True when the authoritative engine did not actually run."""
        return self.source == SOURCE_STREAMING_FALLBACK or bool(self.degraded_reason)

    @property
    def divergent(self) -> bool:
        """True when the two recognisers disagreed on this utterance.

        Compared case-insensitively and whitespace-normalised: those differences
        are not disagreements a human would recognise.
        """
        return _normalise(self.provisional_text) != _normalise(self.authoritative_text)

    def authoritative_for(self, consumer: str, text: str | None = None) -> str:
        """Return the authoritative text, refusing to launder provisional text.

        ``consumer`` names the caller (``"intent"``, ``"reply"``, ``"diary"``,
        ``"graph"``) so a violation names the thing that would have been wrong.
        """
        candidate = self.authoritative_text if text is None else text
        if _normalise(candidate) == _normalise(self.provisional_text) and self.divergent:
            raise ProvisionalTextRejected(
                f"{consumer} was handed the provisional streaming transcript of a "
                f"divergent turn. Use the authoritative transcript "
                f"({self.source!r}); provisional text is for wake detection and "
                f"pre-wake context only."
            )
        return candidate

    # Convenience accessors: every downstream consumer reads one of these.
    def intent_text(self) -> str:
        return self.authoritative_for("intent")

    def reply_text(self) -> str:
        return self.authoritative_for("reply")

    def memory_text(self) -> str:
        return self.authoritative_for("memory")

    def memory_record(self) -> dict[str, Any]:
        """The row written to the diary / knowledge graph for this turn.

        Carries the source and the divergence flag so a retrieved memory can be
        audited later, and deliberately does not carry ``provisional_text``.
        """
        return {
            "text": self.memory_text(),
            "source": self.source,
            "divergent": self.divergent,
            "degraded": self.degraded,
        }

    def tool_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Pass tool arguments through, but reject provisional text inside them.

        Guards the case the reference project was bitten by: a tool call built
        from the streaming transcript while the reply was built from the
        corrected one.
        """
        if not self.divergent:
            return arguments
        provisional = _normalise(self.provisional_text)
        if not provisional:
            return arguments
        for key, value in arguments.items():
            if isinstance(value, str) and _normalise(value) and provisional in _normalise(value):
                raise ProvisionalTextRejected(
                    f"tool argument {key!r} contains provisional streaming text of a "
                    f"divergent turn; build tool calls from the authoritative transcript"
                )
        return arguments

    # -- pre-wake context --------------------------------------------------

    def hearing_context(self, heard: Sequence[str] | Iterable[str]) -> str:
        """Label nearby speech that was *not* addressed to Claire.

        Pre-wake context is something she overheard. Handing it to the model as
        though the user had said it is how a system starts answering questions
        nobody asked, so it is always delivered with this label.
        """
        heard_list = [t for t in heard if t and t.strip()]
        if not heard_list:
            return ""
        joined = " / ".join(t.strip() for t in heard_list)
        return (
            "Overheard nearby, NOT addressed to you and not something the user said "
            f'to you: "{joined}"'
        )

    # -- timings -----------------------------------------------------------

    def timings(self) -> dict[str, float | None]:
        """Latency numbers for this turn, in seconds.

        ``wake_to_endpoint`` is how long the streaming recogniser took to settle
        the utterance; ``first_authoritative_sec`` is the authoritative engine's
        own decode time.
        """
        return {
            "provisional_sec": _delta(self.provisional_started_at, self.provisional_finalized_at),
            "authoritative_sec": _delta(self.authoritative_started_at, self.authoritative_finalized_at),
            "endpoint_to_authoritative_sec": _delta(
                self.provisional_finalized_at, self.authoritative_finalized_at
            ),
        }


def _normalise(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def _delta(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    return round(end - start, 4)
