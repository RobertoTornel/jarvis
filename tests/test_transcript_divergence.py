"""Divergence suite: one utterance must never have two versions (decision D3).

The streaming recogniser is provisional; Parakeet is authoritative. These tests
feed deliberately contradictory transcripts - streaming "book a table for two"
against Parakeet "book a table for four" - and prove every downstream artefact
acts on and remembers the Parakeet text.

No network. No Qt/audio. Pure transcript assembly.
"""

import pytest

from claire.listening.transcript import (
    SOURCE_AUTHORITATIVE_NEMO,
    SOURCE_STREAMING_FALLBACK,
    ProvisionalTextRejected,
    TurnTranscript,
)
from claire.listening.turn import TurnAssembler

PROVISIONAL = "book a table for two"
AUTHORITATIVE = "book a table for four"


@pytest.fixture()
def assembler() -> TurnAssembler:
    return TurnAssembler()


@pytest.fixture()
def divergent_turn(assembler: TurnAssembler) -> TurnTranscript:
    return assembler.build(
        PROVISIONAL,
        AUTHORITATIVE,
        SOURCE_AUTHORITATIVE_NEMO,
    )


def test_tool_call_uses_authoritative_text(
    assembler: TurnAssembler, divergent_turn: TurnTranscript
) -> None:
    """(a) the tool call is built from the Parakeet text, not the stream."""
    arguments = assembler.tool_arguments(
        divergent_turn, {"request": assembler.intent_text(divergent_turn)}
    )
    assert AUTHORITATIVE in arguments["request"]
    assert PROVISIONAL not in arguments["request"]


def test_reply_uses_authoritative_text(
    assembler: TurnAssembler, divergent_turn: TurnTranscript
) -> None:
    """(a) the reply answers the Parakeet text, not the streaming guess."""
    reply = assembler.reply_text(
        divergent_turn, f"Done: {assembler.intent_text(divergent_turn)}"
    )
    assert AUTHORITATIVE in reply
    assert PROVISIONAL not in reply


def test_diary_row_uses_authoritative_text(
    assembler: TurnAssembler, divergent_turn: TurnTranscript
) -> None:
    """(a) the diary row remembers the Parakeet text."""
    row = assembler.diary_row(divergent_turn)
    assert AUTHORITATIVE in row["text"]
    assert PROVISIONAL not in row["text"]


def test_graph_entry_uses_authoritative_text(
    assembler: TurnAssembler, divergent_turn: TurnTranscript
) -> None:
    """(a) the knowledge-graph entry remembers the Parakeet text."""
    entry = assembler.graph_entry(divergent_turn)
    assert AUTHORITATIVE in entry["text"]
    assert PROVISIONAL not in entry["text"]


def test_no_memory_field_contains_streaming_text(
    assembler: TurnAssembler, divergent_turn: TurnTranscript
) -> None:
    """(b) NO memory row contains the streaming text in ANY field."""
    record = assembler.memory_record(divergent_turn)
    for row_name in ("diary", "graph"):
        row = record[row_name]
        for field_name, value in row.items():
            assert PROVISIONAL not in str(value), (
                f"memory row {row_name!r} field {field_name!r} "
                f"leaks provisional text: {value!r}"
            )


@pytest.mark.parametrize(
    "path",
    ["reply", "diary", "graph", "tool"],
)
def test_memory_reply_tool_paths_reject_provisional_text(
    assembler: TurnAssembler, path: str
) -> None:
    """(c) wiring the streaming text into a downstream path RAISES."""
    turn = TurnTranscript(
        provisional_text=PROVISIONAL,
        authoritative_text=AUTHORITATIVE,
        source=SOURCE_AUTHORITATIVE_NEMO,
    )
    with pytest.raises(ProvisionalTextRejected):
        if path == "reply":
            assembler.reply_text(turn, PROVISIONAL)
        elif path == "diary":
            assembler.diary_row(turn, PROVISIONAL)
        elif path == "graph":
            assembler.graph_entry(turn, PROVISIONAL)
        elif path == "tool":
            assembler.tool_arguments(turn, {"request": PROVISIONAL})


def test_authoritative_text_passes_every_guard(
    assembler: TurnAssembler, divergent_turn: TurnTranscript
) -> None:
    """(c, converse) the guard blocks provisional text, not honest use."""
    assert assembler.intent_text(divergent_turn) == AUTHORITATIVE
    assert AUTHORITATIVE in assembler.reply_text(divergent_turn, AUTHORITATIVE)
    assert assembler.diary_row(divergent_turn, AUTHORITATIVE)["text"] == AUTHORITATIVE
    assert assembler.graph_entry(divergent_turn, AUTHORITATIVE)["text"] == AUTHORITATIVE
    assert assembler.tool_arguments(divergent_turn, {"request": AUTHORITATIVE}) == {
        "request": AUTHORITATIVE
    }


def test_hearing_context_labelled_overheard_not_user_speech(
    assembler: TurnAssembler, divergent_turn: TurnTranscript
) -> None:
    """(d) pre-wake context is overheard hearing, never the user's own words."""
    heard = ["what time does the match start", "did you lock the door"]
    context = assembler.hearing_context(divergent_turn, heard)
    bare = " / ".join(heard)
    assert context != bare
    assert "verheard" in context.casefold()
    assert "not" in context.casefold() and "user said" in context.casefold()


def test_build_degraded_marks_transcript_degraded(
    assembler: TurnAssembler,
) -> None:
    """(e) a fallback turn is honestly degraded, never silently verified."""
    reason = "parakeet engine unavailable, promoted streaming transcript"
    turn = assembler.build_degraded(PROVISIONAL, reason)
    assert turn.degraded is True
    assert turn.source == SOURCE_STREAMING_FALLBACK
    assert turn.degraded_reason == reason
    assert turn.source != SOURCE_AUTHORITATIVE_NEMO


def test_memory_record_carries_source_and_divergent(
    assembler: TurnAssembler, divergent_turn: TurnTranscript
) -> None:
    """(f) a stored memory can be audited: source + divergence travel with it."""
    record = assembler.memory_record(divergent_turn)
    for row_name in ("diary", "graph"):
        row = record[row_name]
        assert row["source"] == SOURCE_AUTHORITATIVE_NEMO
        assert row["divergent"] is True
        assert "provisional_text" not in row


def test_build_rejects_empty_authoritative_text(
    assembler: TurnAssembler,
) -> None:
    """An empty Parakeet result is a failure, never a silent 'user said nothing'."""
    with pytest.raises(ValueError):
        assembler.build(PROVISIONAL, "   ", SOURCE_AUTHORITATIVE_NEMO)
