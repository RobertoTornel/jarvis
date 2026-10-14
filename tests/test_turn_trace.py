"""Per-turn latency tracing: one structured record per answered turn.

The contract (``docs/HANDOVER_GENERAL.md`` §7.1) asks for a ``TurnTrace``
stamped at transcription end, router start/end, planner start/end, first and
last LLM token, speech start and first audio byte, written as one bounded JSON
line per turn, mirrored through ``debug_log`` under ``trace``, and readable
from a command. These tests cover:

* the record itself — stages, ordering, route/reasoning/cache fields, and the
  privacy rule that no question or answer text appears without ``voice_debug``;
* the writer — one append per turn, bounded by rotation, and never blocking
  the caller;
* the stamps on the real code paths (engine, router, planner, listener, TTS),
  driven with fakes and no microphone, speaker, network or display;
* the deliberate break: drop one stage's stamp and watch the checker fail, as
  the contract's own verification requires.
"""

import json
import queue
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from claire import turn_trace

pytestmark = pytest.mark.unit


@pytest.fixture
def trace_file(tmp_path, monkeypatch):
    """A private record file for one test, with no trace left in flight."""
    path = tmp_path / "turns.jsonl"
    monkeypatch.setenv(turn_trace.TRACE_PATH_ENV, str(path))
    turn_trace.reset_for_tests()
    yield path
    turn_trace.reset_for_tests()


def _records(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _stamp_all(trace, *, skip=None):
    """Stamp every engine stage in order, minus any named in *skip*."""
    for name in turn_trace.CANONICAL_STAGES:
        if name in (skip or ()):
            continue
        time.sleep(0.0005)
        trace.mark(name)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

class TestTurnTraceRecord:
    def test_every_stage_is_present_and_in_order(self, trace_file):
        turn_trace.note_transcription_end()
        trace = turn_trace.begin_turn("what's the weather")
        _stamp_all(trace)
        trace.set_route("gpt-oss:20b", "gpt-oss:20b")
        trace.set_reasoning("because")
        trace.set_answer("Sunny.")
        turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)

        record = _records(trace_file)[-1]
        assert turn_trace.order_problems(record) == []
        assert turn_trace.expected_stages_present(record) == []
        # transcription_end seeds the turn's zero point, so it is offset 0 and
        # every later stage is strictly positive.
        assert record["stages"]["transcription_end"] == 0.0
        assert all(value > 0 for name, value in record["stages"].items()
                   if name != "transcription_end")

    def test_route_reasoning_and_cache_are_recorded_honestly(self, trace_file):
        trace = turn_trace.begin_turn("hi")
        trace.set_route("requested-model", "served-model")
        trace.set_reasoning("x" * 37)
        turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)

        record = _records(trace_file)[-1]
        assert record["route_requested"] == "requested-model"
        assert record["route_served"] == "served-model"
        assert record["reasoning_present"] is True
        assert record["reasoning_chars"] == 37
        # §7.4's gateway marker does not exist yet: "unknown", never a guess.
        assert record["cache_hit"] is None
        assert record["debug_text"] is False

    def test_served_model_is_left_unset_when_the_response_does_not_name_one(self, trace_file):
        trace = turn_trace.begin_turn("hi")
        trace.set_route("requested-model")
        turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)

        record = _records(trace_file)[-1]
        assert record["route_requested"] == "requested-model"
        assert record["route_served"] is None

    def test_no_question_or_answer_text_without_voice_debug(self, trace_file, monkeypatch):
        monkeypatch.setattr("claire.turn_trace.debug_enabled", lambda: False)
        trace = turn_trace.begin_turn("my secret question")
        trace.set_answer("my secret answer")
        turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)

        raw = trace_file.read_text()
        assert "secret" not in raw
        record = _records(trace_file)[-1]
        assert "question" not in record and "answer" not in record
        assert record["question_chars"] == len("my secret question")
        assert record["answer_chars"] == len("my secret answer")

    def test_text_appears_only_when_voice_debug_is_on(self, trace_file, monkeypatch):
        monkeypatch.setattr("claire.turn_trace.debug_enabled", lambda: True)
        trace = turn_trace.begin_turn("my secret question")
        trace.set_answer("my secret answer")
        turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)

        record = _records(trace_file)[-1]
        assert record["debug_text"] is True
        assert record["question"] == "my secret question"
        assert record["answer"] == "my secret answer"

    def test_one_line_summary_is_mirrored_under_the_trace_category(self, trace_file, monkeypatch):
        logged = []
        monkeypatch.setattr(
            "claire.turn_trace.debug_log",
            lambda message, category="debug": logged.append((category, message)),
        )
        trace = turn_trace.begin_turn("a question nobody should read")
        _stamp_all(trace)
        trace.set_answer("an answer nobody should read")
        turn_trace.finish_turn(trace)

        assert len(logged) == 1, "one summary line per turn, no more"
        category, message = logged[0]
        assert category == "trace"
        assert "\n" not in message
        assert "total=" in message and "router=" in message and "route=" in message
        assert "unknown" in message  # the cache marker is honest
        assert "nobody should read" not in message

    def test_mark_keeps_the_first_stamp_and_mark_latest_replaces_it(self):
        trace = turn_trace.TurnTrace(turn_id="t", started_monotonic=time.monotonic())
        trace.mark("router_start", 10.0)
        trace.mark("router_start", 20.0)
        assert trace.marks["router_start"] == 10.0
        trace.mark_latest("llm_last_token", 30.0)
        trace.mark_latest("llm_last_token", 40.0)
        assert trace.marks["llm_last_token"] == 40.0

    def test_a_misspelled_stage_is_refused_rather_than_silently_kept(self):
        trace = turn_trace.TurnTrace(turn_id="t", started_monotonic=time.monotonic())
        assert trace.mark("router_ends") is None
        assert trace.marks == {}

    def test_total_matches_a_wall_clock(self, trace_file):
        """A second clock, so a nanoseconds-or-milliseconds mix-up is caught."""
        wall_start = time.time()
        trace = turn_trace.begin_turn("q")
        time.sleep(0.12)
        trace.mark("llm_last_token")
        wall_elapsed = time.time() - wall_start
        record = trace.to_record()
        assert record["total_s"] == pytest.approx(wall_elapsed, abs=0.03)

    def test_llm_span_falls_back_to_routing_when_the_planner_was_skipped(self):
        trace = turn_trace.TurnTrace(turn_id="t", started_monotonic=0.0)
        trace.mark("llm_last_token", 10.0)
        assert trace.llm_seconds() is None
        trace.mark("router_end", 5.0)
        assert trace.llm_seconds() == 5.0
        trace.mark("planner_end", 6.0)
        assert trace.llm_seconds() == 4.0

    def test_running_total_is_measured_not_summed(self, trace_file):
        start = time.monotonic()
        trace = turn_trace.begin_turn("hi")
        trace.mark("transcription_end", start)
        trace.mark("llm_last_token", start + 1.5)
        record = trace.to_record()
        assert record["total_s"] == pytest.approx(1.5, abs=0.01)


# ---------------------------------------------------------------------------
# The ordering checker
# ---------------------------------------------------------------------------

class TestOrderChecker:
    def _record(self, stages):
        return {"stages": stages, "turn_id": "t"}

    def test_missing_stage_is_reported(self):
        problems = turn_trace.order_problems(
            self._record({"transcription_end": 0.0, "router_start": 0.1})
        )
        assert "missing stage router_end" in problems

    def test_out_of_order_stage_is_reported(self):
        stages = {name: float(index) for index, name in enumerate(turn_trace.CANONICAL_STAGES)}
        stages["router_end"] = 0.0
        stages["router_start"] = 5.0
        problems = turn_trace.order_problems(self._record(stages))
        assert any("router_end" in problem and "precedes" in problem for problem in problems)

    def test_empty_record_does_not_pass_by_vacuity(self):
        problems = turn_trace.order_problems({"stages": {}})
        assert len(problems) == len(turn_trace.CANONICAL_STAGES)


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------

class TestWriter:
    def test_one_line_per_turn_and_one_write(self, trace_file):
        for index in range(3):
            trace = turn_trace.begin_turn(f"q{index}")
            _stamp_all(trace)
            turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)
        lines = [line for line in trace_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 3

    def test_finishing_twice_writes_one_record(self, trace_file):
        trace = turn_trace.begin_turn("hi")
        turn_trace.finish_turn(trace)
        turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)
        assert len(_records(trace_file)) == 1

    def test_file_is_bounded_by_rotation(self, trace_file, monkeypatch):
        monkeypatch.setattr(turn_trace, "MAX_TRACE_FILE_BYTES", 2000)
        for index in range(50):
            trace = turn_trace.begin_turn(f"question {index}")
            _stamp_all(trace)
            turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)

        backup = trace_file.with_name(trace_file.name + ".1")
        assert backup.exists(), "the file must rotate rather than grow for ever"
        total = sum(path.stat().st_size for path in (trace_file, backup) if path.exists())
        # 50 records are ~30 kB; a bounded writer keeps the two files near the
        # 2 kB rotation point instead of growing with the number of turns.
        assert total < 4 * 2000
        records = turn_trace.read_records()
        assert records[-1]["question_chars"] == len("question 49")
        assert len(records) >= 2

    def test_submitting_to_a_full_queue_drops_rather_than_blocks(self, trace_file):
        writer = turn_trace._TraceWriter(trace_file)
        writer._start_thread_locked = lambda: None  # no drain: the queue stays full
        for _ in range(turn_trace._WRITE_QUEUE_MAX):
            writer.submit({"turn_id": "x"})
        assert writer._q.full()
        started = time.monotonic()
        writer.submit({"turn_id": "dropped"})
        assert time.monotonic() - started < 0.5, "a turn must never wait on the writer"
        assert writer.dropped == 1

    def test_disabled_tracing_writes_nothing(self, trace_file, monkeypatch):
        monkeypatch.setenv(turn_trace.TRACE_DISABLED_ENV, "1")
        trace = turn_trace.begin_turn("hi")
        turn_trace.finish_turn(trace)
        turn_trace.flush(2.0)
        assert not trace_file.exists()


# ---------------------------------------------------------------------------
# The stamps on the real paths
# ---------------------------------------------------------------------------

class _FakeBackend:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, model, messages, **kwargs):
        self.calls.append(model)
        return self.response


def _llm_response(text="Sunny tomorrow.", *, model="served-by-the-gateway",
                  reasoning="thinking hard"):
    message = {"role": "assistant", "content": text}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    response = {"message": message}
    if model is not None:
        response["model"] = model
    return response


class TestEngineStamps:
    def test_text_turn_records_engine_stages_route_and_reasoning(
        self, trace_file, mock_config, db, dialogue_memory, monkeypatch
    ):
        from claire.reply import engine as engine_mod

        mock_config.tool_selection_strategy = "all"
        mock_config.llm_chat_model = "requested-model"
        monkeypatch.setattr(engine_mod, "get_llm_backend", lambda cfg: _FakeBackend(_llm_response()))
        monkeypatch.setattr(
            "claire.reply.planner.call_llm_direct",
            lambda **kwargs: "Reply to the user.",
        )

        reply = engine_mod.run_reply_engine(
            db, mock_config, None, "what's the weather", dialogue_memory, quiet=True
        )
        assert reply == "Sunny tomorrow."

        # The engine scheduled the write; the test closes the turn now rather
        # than waiting out the engine's safety window.
        turn_trace.finish_turn()
        assert turn_trace.flush(2.0)

        record = _records(trace_file)[-1]
        assert turn_trace.expected_stages_present(record, voice=False) == []
        # A unit mix-up (nanoseconds, milliseconds) would show up as a total
        # nowhere near the seconds this fake turn actually took.
        assert 0 < record["total_s"] < 10
        assert record["route_requested"] == "requested-model"
        assert record["route_served"] == "served-by-the-gateway"
        assert record["reasoning_present"] is True
        assert record["reasoning_chars"] == len("thinking hard")
        assert record["answer_chars"] == len("Sunny tomorrow.")
        assert record["question_chars"] == len("what's the weather")

    def test_router_and_planner_stamps_come_from_their_own_functions(
        self, trace_file, mock_config, db, dialogue_memory, monkeypatch
    ):
        from claire.reply import engine as engine_mod
        from claire.tools.selection import select_tools, ToolSelectionStrategy
        from claire.reply import planner as planner_mod

        trace = turn_trace.begin_turn("what's the weather in london")
        select_tools(
            query="what's the weather in london",
            builtin_tools={"stop": object()},
            mcp_tools={},
            strategy=ToolSelectionStrategy.ALL,
        )
        monkeypatch.setattr(
            planner_mod, "call_llm_direct", lambda **kwargs: "Search the web."
        )
        planner_mod.plan_query(
            cfg=mock_config, query="what's the weather in london",
            dialogue_context="", tools=[("stop", "stop")],
        )
        record = trace.to_record()
        assert record["durations"]["router_s"] is not None
        assert record["durations"]["planner_s"] is not None
        assert record["stages"]["router_start"] <= record["stages"]["router_end"]
        assert record["stages"]["planner_start"] <= record["stages"]["planner_end"]

    def test_route_requested_is_the_model_the_engine_asked_for(
        self, trace_file, mock_config, monkeypatch
    ):
        from claire.reply import engine as engine_mod

        backend = _FakeBackend(_llm_response(model=None))
        monkeypatch.setattr(engine_mod, "get_llm_backend", lambda cfg: backend)
        mock_config.llm_chat_model = "requested-model"

        trace = turn_trace.begin_turn("q")
        engine_mod.chat_with_messages(mock_config, [{"role": "user", "content": "q"}])
        record = trace.to_record()
        assert record["route_requested"] == "requested-model"
        assert record["route_served"] is None
        assert record["stages"]["llm_last_token"] >= record["stages"]["llm_first_token"]

    def test_missing_planner_end_stamp_makes_the_checker_fail(
        self, trace_file, mock_config, db, dialogue_memory, monkeypatch
    ):
        """The contract's own break: drop one stamp and watch it be caught."""
        from claire.reply import engine as engine_mod
        from claire.reply import planner as planner_mod

        mock_config.tool_selection_strategy = "all"
        monkeypatch.setattr(engine_mod, "get_llm_backend", lambda cfg: _FakeBackend(_llm_response()))

        real_plan_query = planner_mod.plan_query

        def plan_query_missing_end(*args, **kwargs):
            # planner_start is stamped by the decorator; planner_end is not.
            result = real_plan_query.__wrapped__(*args, **kwargs)
            turn_trace.mark("planner_start")
            return result

        monkeypatch.setattr(engine_mod, "plan_query", plan_query_missing_end)
        monkeypatch.setattr(planner_mod, "call_llm_direct", lambda **kwargs: "Reply to the user.")

        engine_mod.run_reply_engine(
            db, mock_config, None, "what's the weather", dialogue_memory, quiet=True
        )
        turn_trace.finish_turn()
        turn_trace.flush(2.0)

        record = _records(trace_file)[-1]
        problems = turn_trace.order_problems(record)
        assert "missing stage planner_end" in problems


# ---------------------------------------------------------------------------
# The listener and the speaker
# ---------------------------------------------------------------------------

def _transcription_result(text, *, generation):
    now = time.time()
    return SimpleNamespace(
        text=text, language="en", low_confidence_events=(),
        start_time=now - 1.0, end_time=now, energy=0.5,
        capture_generation=generation, captured_during_tts=False,
        captured_tts_start_time=0.0, turn=None, provisional_text="",
    )


class TestListenerAndSpeakerStamps:
    def test_transcription_end_is_recorded_by_the_listener(self, monkeypatch):
        from test_hot_window_input import _create_listener

        listener, _ = _create_listener()
        listener._first_utterance = True
        listener._transcript_buffer.add = lambda *args, **kwargs: None
        listener._process_transcript = lambda *args, **kwargs: None
        listener._emit_low_confidence = lambda *args, **kwargs: None
        listener._is_repetitive_hallucination = lambda text: False

        listener._handle_transcription_result(
            _transcription_result("hello there", generation=listener._capture_generation)
        )

        trace = turn_trace.begin_turn("hello there")
        try:
            assert trace.marks["transcription_end"] == trace.started_monotonic
            assert trace.offset_of("transcription_end") == 0.0
        finally:
            turn_trace.finish_turn(trace)

    def test_dispatch_stamps_speech_start_after_the_reply_is_ready(self, monkeypatch):
        from test_hot_window_input import _create_listener

        listener, _tts = _create_listener()
        listener.cfg.ack_phrases = ["On it."]
        listener.cfg.ack_enabled = False
        monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

        def fake_engine(db, cfg, tts, text, dialogue_memory, **kwargs):
            trace = turn_trace.begin_turn(text)
            for name in turn_trace.CANONICAL_STAGES[:7]:
                time.sleep(0.0005)
                trace.mark(name)
            trace.set_answer("Sunny tomorrow.")
            return "Sunny tomorrow."

        monkeypatch.setattr("claire.reply.engine.run_reply_engine", fake_engine)
        turn_trace.note_transcription_end()

        listener._dispatch_query("what's the weather")

        turn_trace.finish_turn()
        assert turn_trace.flush(2.0)
        record = turn_trace.read_records()[-1]
        assert record["stages"]["transcription_end"] == 0.0
        assert record["stages"]["speech_start"] > record["stages"]["llm_last_token"]

    def test_first_audio_byte_is_ignored_until_speech_start_is_stamped(self, trace_file):
        trace = turn_trace.begin_turn("q")
        for name in turn_trace.CANONICAL_STAGES[:7]:
            trace.mark(name)
        # The acknowledgement clip can still be draining from before the
        # reply: no byte is attributed to the reply before speech_start.
        assert turn_trace.mark_reply_audio_start() is False
        assert "first_audio_byte" not in trace.marks
        turn_trace.mark("speech_start")
        assert turn_trace.mark_reply_audio_start() is True
        assert turn_trace.flush(2.0)
        assert _records(trace_file)[-1]["stages"]["first_audio_byte"] is not None
        # The turn is closed by that byte: the last named stage is in.
        assert trace.finished


# ---------------------------------------------------------------------------
# The command and the control socket
# ---------------------------------------------------------------------------

class TestReaderAndSocket:
    def test_cli_reads_the_last_records_and_checks_order(self, trace_file, capsys):
        import importlib.util

        script = Path(__file__).resolve().parent.parent / "scripts" / "turn_trace.py"
        spec = importlib.util.spec_from_file_location("turn_trace_cli", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        trace = turn_trace.begin_turn("q")
        _stamp_all(trace)
        turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)

        assert module.main(["--last", "1", "--check"]) == 0
        out = capsys.readouterr().out
        assert "turn trace" in out and "all stages" in out

    def test_cli_reports_a_broken_record(self, trace_file, capsys):
        import importlib.util

        script = Path(__file__).resolve().parent.parent / "scripts" / "turn_trace.py"
        spec = importlib.util.spec_from_file_location("turn_trace_cli", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        trace = turn_trace.begin_turn("q")
        _stamp_all(trace, skip=("planner_end",))
        turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)

        assert module.main(["--last", "1", "--check"]) == 1
        assert "missing stage planner_end" in capsys.readouterr().out

    def test_control_socket_answers_a_turn_trace_request(self, trace_file, capsys):
        from claire import daemon

        trace = turn_trace.begin_turn("q")
        _stamp_all(trace)
        turn_trace.finish_turn(trace)
        assert turn_trace.flush(2.0)

        assert daemon.handle_turn_trace_stdin_line('{"last": 3}') is False
        assert daemon.handle_turn_trace_stdin_line('__TURN_TRACE__:{"last": 1}') is True
        emitted = capsys.readouterr().out
        assert "__TURN_TRACE__:" in emitted
        payload = json.loads(emitted.strip().split("__TURN_TRACE__:", 1)[1])
        assert payload["type"] == "records"
        assert len(payload["data"]["records"]) == 1
        assert payload["data"]["records"][0]["turn_id"] == trace.turn_id

    def test_control_socket_malformed_payload_is_consumed_not_ignored(self, capsys):
        from claire import daemon

        assert daemon.handle_turn_trace_stdin_line("__TURN_TRACE__:{not json") is True
        capsys.readouterr()
