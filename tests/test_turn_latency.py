"""Per-turn latency: one honest journal line for every turn Claire answers.

Two layers are under test here:

* ``claire.listening.latency`` — the pure formatter, where the honesty rules
  live: four measured numbers, ``-`` where nothing was measured (never a
  plausible zero), and a total printed as measured rather than recomputed
  from its parts.
* the listener wiring — ``_dispatch_query`` prints exactly one line for a
  turn it answers, prints nothing for a turn that is never dispatched, a
  dispatch with no measured values prints dashes rather than zeros, and the
  decode duration recorded with a heard transcript is the one the line
  reports.

Everything runs with fakes: no model, no microphone, no network, no display
and no sleeps. The listener fixture is ``_create_listener`` from the
hot-window suite — a real ``VoiceListener`` with real ``StateManager`` and
``EchoDetector``, mocked heavy subsystems.
"""

import re
import time
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from claire.listening.latency import format_turn_line, measured_stt_seconds
from claire.listening.transcript import SOURCE_AUTHORITATIVE_ONNX
from claire.listening.turn import TurnAssembler

from test_hot_window_input import _create_listener

pytestmark = pytest.mark.unit

#: The emoji that opens the journal's latency line.
_LATENCY_PREFIX = "⏱️"


# ---------------------------------------------------------------------------
# The pure formatter
# ---------------------------------------------------------------------------

class TestFormatTurnLine:
    """The formatter renders what it is given and nothing else."""

    def test_full_set_renders_all_four_numbers(self):
        silence, stt, reply = 2.0, 0.9, 1.8
        line = format_turn_line(
            silence=silence, stt=stt, reply=reply, total=silence + stt + reply,
            query="what's the weather",
        )
        assert line == (
            '⏱️ turn 4.7s: silence 2.0s + stt 0.9s + reply 1.8s '
            '— "what\'s the weather"'
        )

    def test_missing_value_renders_dash_not_zero(self):
        # Every field unmeasured: four dashes, no zeros. Swapping any dash
        # for 0.0s would claim a measurement that was never taken — that is
        # precisely the regression this test exists to catch.
        line = format_turn_line(
            silence=None, stt=None, reply=None, total=None, query="hi",
        )
        assert line == '⏱️ turn -: silence - + stt - + reply - — "hi"'
        assert "0.0" not in line

    def test_partial_missing_keeps_the_measured_fields(self):
        # A decode that never ran must not blank out the spans that were
        # measured, and must not print 0.0 in its place.
        line = format_turn_line(
            silence=2.0, stt=None, reply=1.8, total=3.8, query="hi",
        )
        assert "turn 3.8s" in line
        assert "silence 2.0s" in line
        assert "stt -" in line
        assert "reply 1.8s" in line
        assert "stt 0" not in line

    def test_total_is_consistent_with_its_parts(self):
        # The canonical case: the caller measured a total equal to the sum of
        # its parts, and the line shows exactly that arithmetic — all four
        # numbers present, the total agreeing with them.
        silence, stt, reply = 2.0, 0.9, 1.8
        total = silence + stt + reply
        line = format_turn_line(
            silence=silence, stt=stt, reply=reply, total=total,
            query="what's the weather",
        )
        assert f"turn {total:.1f}s" in line
        shown_total = float(re.search(r"turn (\d+\.\d+)s", line).group(1))
        shown_parts = [
            float(re.search(rf"{label} (\d+\.\d+)s", line).group(1))
            for label in ("silence", "stt", "reply")
        ]
        assert shown_total == pytest.approx(sum(shown_parts), abs=0.05)

    def test_total_is_the_measured_span_not_a_recomputed_sum(self):
        # The parts do NOT sum to the total in general: the decode finishes
        # inside the silence window and the reply engine can wait on the
        # query lock. The formatter must print the total it was handed —
        # a sum here would be an invented number.
        line = format_turn_line(
            silence=4.5, stt=0.7, reply=1.6, total=6.4, query="hi",
        )
        assert "turn 6.4s" in line
        assert "turn 6.8s" not in line

    def test_query_is_collapsed_to_one_line(self):
        line = format_turn_line(
            silence=1.0, stt=0.5, reply=1.0, total=2.5,
            query='say  "hi"\nplease\tnow',
        )
        assert "\n" not in line
        # Quotes inside the utterance cannot break the line's structure: the
        # query is one spaced run and its own double quotes become apostrophes.
        assert line.endswith('— "say \'hi\' please now"')


# ---------------------------------------------------------------------------
# Which decode duration the line is allowed to report
# ---------------------------------------------------------------------------

class TestMeasuredSttSeconds:
    """Only an authoritative decode that actually ran may be timed."""

    def test_authoritative_decode_duration_is_measured(self):
        turn = TurnAssembler().build(
            "claire whats the weather",
            "what's the weather",
            SOURCE_AUTHORITATIVE_ONNX,
            authoritative_started_at=5.0,
            authoritative_finalized_at=5.9,
        )
        assert measured_stt_seconds(turn) == pytest.approx(0.9)

    def test_no_turn_has_no_duration(self):
        assert measured_stt_seconds(None) is None

    def test_degraded_turn_reports_no_duration_even_with_timestamps(self):
        # A degraded turn's text came from the provisional/streaming source:
        # whatever duration is attached, it is not this transcript's decode.
        turn = replace(
            TurnAssembler().build_degraded("claire whats the weather", "reason"),
            authoritative_started_at=5.0,
            authoritative_finalized_at=5.9,
        )
        assert measured_stt_seconds(turn) is None

    def test_turn_without_authoritative_timings_reports_no_duration(self):
        turn = TurnAssembler().build(
            "claire whats the weather",
            "what's the weather",
            SOURCE_AUTHORITATIVE_ONNX,
        )
        assert measured_stt_seconds(turn) is None


# ---------------------------------------------------------------------------
# The line the journal actually shows
# ---------------------------------------------------------------------------

def _install_fast_reply(monkeypatch, reply="Sunny tomorrow."):
    """Answer every dispatch instantly, without touching the real engine."""
    monkeypatch.setattr(
        "claire.reply.engine.run_reply_engine",
        lambda *args, **kwargs: reply,
    )
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)


def _latency_lines(stdout: str) -> list:
    return [line for line in stdout.splitlines() if line.startswith(_LATENCY_PREFIX)]


def _transcription_result(turn, *, generation: int) -> SimpleNamespace:
    """A transcription result shaped like the worker's, for direct delivery."""
    now = time.time()
    return SimpleNamespace(
        text=turn.authoritative_text,
        language="en",
        low_confidence_events=(),
        start_time=now - 1.0,
        end_time=now,
        energy=0.5,
        capture_generation=generation,
        captured_during_tts=False,
        captured_tts_start_time=0.0,
        turn=turn,
        provisional_text=turn.provisional_text,
    )


class TestLatencyLineOnDispatch:
    """One line per answered turn, none for a turn that is never dispatched."""

    def test_answered_turn_prints_one_latency_line(self, monkeypatch, capsys):
        _install_fast_reply(monkeypatch)
        listener, _ = _create_listener()
        listener.cfg.ack_phrases = ["On it."]
        listener.state_manager.note_voice_heard(time.time() - 2.5)
        listener._turn_stt_seconds = 0.9  # recorded with the final transcript

        listener._dispatch_query("what's the weather")

        lines = _latency_lines(capsys.readouterr().out)
        assert len(lines) == 1, lines
        match = re.fullmatch(
            r'⏱️ turn (\d+\.\d+)s: silence (\d+\.\d+)s \+ stt 0\.9s '
            r'\+ reply (\d+\.\d+)s — "what\'s the weather"',
            lines[0],
        )
        assert match, lines[0]
        total, silence, reply = (float(match.group(i)) for i in (1, 2, 3))
        # Silence runs from the last word (2.5 s ago) to this dispatch; the
        # total runs from the last word to the reply being ready, so it can
        # never be shorter than the silence it contains.
        assert silence == pytest.approx(2.5, abs=0.1)
        assert reply >= 0.0
        assert total >= silence

    def test_turn_that_is_never_dispatched_prints_no_line(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "claire.reply.engine.run_reply_engine",
            lambda *args, **kwargs: pytest.fail(
                "the reply engine must not run for a turn that is never dispatched",
            ),
        )
        monkeypatch.setattr("claire.daemon.query_lock", nullcontext)
        listener, _ = _create_listener()
        listener.cfg.ack_phrases = ["On it."]
        listener.state_manager.note_voice_heard(time.time())
        listener.state_manager.start_collection("claire what's the weather")

        # The collection window has not expired, so this turn is heard but
        # never dispatched — the journal gains no latency line for the many
        # utterances Claire does not act on.
        listener._check_query_timeout()

        assert _latency_lines(capsys.readouterr().out) == []

    def test_dispatch_without_measured_values_prints_dashes(self, monkeypatch, capsys):
        # No voice moment was ever noted and no decode was recorded for this
        # turn: three dashes, not three zeroes. The reply duration is real,
        # so it still appears.
        _install_fast_reply(monkeypatch)
        listener, _ = _create_listener()
        listener.cfg.ack_phrases = ["On it."]

        listener._dispatch_query("what's the weather")

        lines = _latency_lines(capsys.readouterr().out)
        assert len(lines) == 1, lines
        assert re.fullmatch(
            r'⏱️ turn -: silence - \+ stt - \+ reply \d+\.\d+s '
            r'— "what\'s the weather"',
            lines[0],
        ), lines[0]

    def test_heard_transcript_records_its_decode_for_the_next_line(self, capsys):
        # The production path for the stt field: a transcription result whose
        # turn carried a measured authoritative decode leaves that duration in
        # the listener's per-turn state, ready for _dispatch_query to read.
        listener, _ = _create_listener()
        turn = TurnAssembler().build(
            "claire whats the weather",
            "what's the weather",
            SOURCE_AUTHORITATIVE_ONNX,
            authoritative_started_at=5.0,
            authoritative_finalized_at=5.9,
        )
        result = _transcription_result(turn, generation=listener._capture_generation)

        listener._handle_transcription_result(result)

        assert listener._turn_stt_seconds == pytest.approx(0.9)
        capsys.readouterr()  # discard the 📝 Heard line this path also prints
