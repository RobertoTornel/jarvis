"""A supplied location continues a stalled time request.

The first turn names a place getTime cannot resolve and the second supplies
London. Both turns must return usable replies, the follow-up must re-invoke
getTime with the location the user supplied, and the final answer must use
the tool's local time. An unrelated film search result cannot substitute for
time evidence.
"""

from unittest.mock import patch

import pytest

from evals.memory_tool_grounding import assert_time_reply, assert_usable_answer

from conftest import requires_judge_llm
from helpers import (
    ToolCallCapture,
    JUDGE_MODEL,
    voice_config,
)


_LONDON_TIME = """Current time in London, United Kingdom:
Timezone: Europe/London
Local time: 17:42
Date: Sunday, 2026-04-20
"""


def _make_get_time_runner(capture: ToolCallCapture):
    """Mock for ``run_tool_with_retries`` that responds to getTime based
    on the location argument.

    Empty args → ``success=True`` with the user's own local time, matching
    the real getTime behaviour (``location`` is optional and auto-derives).
    ``location='London'`` → ``success=True`` plus the canned local time.
    Any other location fails the way the real tool fails on an unresolvable
    place, which stamps ``tool_failed=True`` on the recorded tool turn so
    the engine's carry-over guard widens the next turn's allow-list.
    If the model misroutes to webSearch we want to make damn sure we don't
    accidentally satisfy the assertion via a confabulated success — return
    something the model cannot honestly turn into London's local time.
    """
    from claire.tools.types import ToolExecutionResult

    def _runner(db, cfg, tool_name, tool_args, **kwargs):
        capture.record(tool_name, tool_args or {})
        if tool_name == "getTime":
            location = ((tool_args or {}).get("location") or "").strip()
            if not location:
                return ToolExecutionResult(
                    success=True,
                    reply_text=(
                        "Current time (your location):\n"
                        "Timezone: UTC\n"
                        "Local time: 20:42\n"
                    ),
                )
            if "london" in location.casefold():
                return ToolExecutionResult(
                    success=True,
                    reply_text=_LONDON_TIME,
                )
            return ToolExecutionResult(
                success=False,
                reply_text=(
                    f"Could not find location '{location}'. Try a different "
                    "city name or spelling."
                ),
            )
        if tool_name == "webSearch":
            return ToolExecutionResult(
                success=True,
                reply_text=(
                    "UNTRUSTED WEB EXTRACT:\n"
                    "Edge of Tomorrow is a 2014 American science fiction "
                    "action film directed by Doug Liman, starring Tom Cruise."
                ),
            )
        return ToolExecutionResult(success=True, reply_text="OK")

    return _runner


@pytest.mark.eval
@requires_judge_llm
class TestFollowupSuppliesMissingToolArg:
    """End-to-end regression for the engine-level tool carry-over guard."""

    def test_short_followup_continues_previous_tool_chain(
        self, eval_db, eval_dialogue_memory,
    ):
        from claire.reply.engine import run_reply_engine

        cfg = voice_config()
        # Geoip disabled — the only way getTime learns a place is
        # from the user supplying one on either turn.
        cfg.location_enabled = False

        capture = ToolCallCapture()

        with patch(
            "claire.reply.engine.run_tool_with_retries",
            side_effect=_make_get_time_runner(capture),
        ):
            turn1 = run_reply_engine(
                db=eval_db, cfg=cfg, tts=None,
                text="what time is it in Kazbegi Claire?",
                dialogue_memory=eval_dialogue_memory,
            )
            turn2 = run_reply_engine(
                db=eval_db, cfg=cfg, tts=None,
                text="I'm in London",
                dialogue_memory=eval_dialogue_memory,
            )

        print(f"\n  🔁 Follow-up Carry-over ({JUDGE_MODEL}):")
        print(f"  💬 Turn 1 reply: {(turn1 or '')[:200]}")
        print(f"  💬 Turn 2 reply: {(turn2 or '')[:200]}")
        print(f"  🛠️ Tools called: {capture.tool_names()}")
        for c in capture.calls:
            print(f"    🔧 {c['name']}({c['args']})")

        assert_usable_answer(turn1, "turn-1")
        assert_time_reply(turn2, _LONDON_TIME, "turn-2")

        time_calls = [c for c in capture.calls if c["name"] == "getTime"]
        # Turn-2 call must carry the location the user supplied.
        london_calls = [
            c for c in time_calls
            if "london" in (c["args"].get("location") or "").lower()
        ]
        assert london_calls, (
            "getTime was never re-invoked with location='London' on "
            "turn 2 — the carry-over guard did not preserve the previous "
            f"tool's place in the allow-list. All getTime calls: "
            f"{[c['args'] for c in time_calls]}"
        )

        # webSearch must NOT have been the path — that's the field-trace
        # failure mode (Edge of Tomorrow). If it fired anyway, the user
        # answer must still be about London's time, not the film.
        turn2_lower = (turn2 or "").lower()
        assert "edge of tomorrow" not in turn2_lower, (
            "Reply parroted the Wikipedia fallback for 'Edge of Tomorrow' "
            f"instead of using the time tool: {turn2}"
        )
        assert "london" in turn2_lower, (
            f"Turn 2 must answer about London, got: {turn2}"
        )
