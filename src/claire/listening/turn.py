"""One utterance, one truth: assemble a turn from its two transcripts.

Decision D3 gives Claire two recognisers — streaming (provisional) and Parakeet
(authoritative) — and the reference project shipped the defect this module
exists to prevent: downstream artefacts built from the streaming guess while
the turn itself carried corrected text, so one utterance had two versions.

:class:`TurnAssembler` is deliberately thin. It builds :class:`TurnTranscript`
turns and derives every downstream artefact through the transcript's guarded
accessors (:meth:`authoritative_for`, :meth:`tool_arguments`,
:meth:`hearing_context`), which raise :class:`ProvisionalTextRejected` if
provisional text is ever wired into intent, tools, reply or memory. No
Qt/audio/network; no persistence here — callers write the rows this returns.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .transcript import (
    SOURCE_STREAMING_FALLBACK,
    TurnTranscript,
)


class TurnAssembler:
    """Build turns and derive their intent/tool/reply/memory artefacts."""

    def build(
        self,
        provisional: str,
        authoritative: str,
        source: str,
        **timings: float | None,
    ) -> TurnTranscript:
        """Assemble a verified turn. Empty Parakeet output is a failure."""
        if not (authoritative or "").strip():
            raise ValueError(
                "authoritative transcript is empty: the engine failed, "
                "which must never look like 'the user said nothing'. "
                f"(source={source!r})"
            )
        return TurnTranscript(
            provisional_text=provisional or "",
            authoritative_text=authoritative,
            source=source,
            **timings,
        )

    def build_degraded(self, provisional: str, reason: str) -> TurnTranscript:
        """Promote streaming text when Parakeet did not run — marked honestly."""
        if not (provisional or "").strip():
            raise ValueError(
                "cannot build even a degraded turn from empty streaming text."
            )
        return TurnTranscript(
            provisional_text=provisional,
            authoritative_text=provisional,
            source=SOURCE_STREAMING_FALLBACK,
            degraded_reason=reason,
        )

    def intent_text(self, turn: TurnTranscript, text: str | None = None) -> str:
        return turn.authoritative_for("intent", text)

    def tool_arguments(
        self, turn: TurnTranscript, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return turn.tool_arguments(arguments)

    def reply_text(self, turn: TurnTranscript, text: str | None = None) -> str:
        return turn.authoritative_for("reply", text)

    def diary_row(self, turn: TurnTranscript, text: str | None = None) -> dict[str, Any]:
        return {
            "text": turn.authoritative_for("diary", text),
            "source": turn.source,
            "divergent": turn.divergent,
            "degraded": turn.degraded,
        }

    def graph_entry(
        self, turn: TurnTranscript, text: str | None = None
    ) -> dict[str, Any]:
        return {
            "text": turn.authoritative_for("graph", text),
            "source": turn.source,
            "divergent": turn.divergent,
            "degraded": turn.degraded,
        }

    def memory_record(self, turn: TurnTranscript) -> dict[str, Any]:
        return {"diary": self.diary_row(turn), "graph": self.graph_entry(turn)}

    def hearing_context(
        self, turn: TurnTranscript, heard: Sequence[str] | Iterable[str]
    ) -> str:
        return turn.hearing_context(heard)
