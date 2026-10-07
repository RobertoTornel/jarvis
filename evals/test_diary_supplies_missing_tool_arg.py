"""Recalled diary location facts ground time tool arguments and answers.

GeoIP is disabled and the hot window is empty. The diary supplies Manchester
through long-term memory enrichment. Model failures remain visible.
"""

from unittest.mock import patch

import pytest

from evals.memory_tool_grounding import assert_time_reply

from conftest import requires_judge_llm
from helpers import (
    ToolCallCapture,
    seed_diary_summaries,
    JUDGE_MODEL,
    voice_config,
)


_DIARY_MANCHESTER = [
    (
        "2026-04-26",
        "The user mentioned they live in Manchester and prefer the 24-hour "
        "clock for time queries.",
    ),
]


_MANCHESTER_TIME = """Current time in Manchester, United Kingdom:
Timezone: Europe/London
Local time: 12:15
Date: Sunday, 2026-04-26
"""


def _make_runner(capture: ToolCallCapture):
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
                        "Local time: 11:15\n"
                    ),
                )
            if "manchester" not in location.casefold():
                return ToolExecutionResult(
                    success=False,
                    reply_text=(
                        f"Could not find location '{location}'. Try a "
                        "different city name or spelling."
                    ),
                )
            return ToolExecutionResult(
                success=True,
                reply_text=_MANCHESTER_TIME,
            )
        return ToolExecutionResult(success=True, reply_text="OK")

    return _runner

@pytest.mark.eval
@requires_judge_llm
class TestDiarySuppliesMissingToolArg:
    """Diary-recall path: location surfaced from a prior conversation
    grounds the getTime call without needing the hot window or
    explicit user re-statement."""

    def test_diary_location_grounds_time_call(
        self, eval_db, eval_dialogue_memory,
    ):
        from claire.reply.engine import run_reply_engine

        cfg = voice_config()
        # Geoip disabled — the only way the model gets a location is from
        # diary recall.
        cfg.location_enabled = False
        cfg.memory_enrichment_source = "diary"

        seed_diary_summaries(eval_db, _DIARY_MANCHESTER)

        capture = ToolCallCapture()

        with patch(
            "claire.reply.engine.run_tool_with_retries",
            side_effect=_make_runner(capture),
        ):
            response = run_reply_engine(
                db=eval_db, cfg=cfg, tts=None,
                text="what time is it where I live, Claire?",
                dialogue_memory=eval_dialogue_memory,
            )

        print(f"\n  📖 Diary Supplies Missing Tool Arg ({JUDGE_MODEL}):")
        print(f"  🛠️ Tools called: {capture.tool_names()}")
        for c in capture.calls:
            print(f"    🔧 {c['name']}({c['args']})")
        print(f"  💬 Response: {(response or '')[:300]}")

        assert_time_reply(response, _MANCHESTER_TIME, "diary-recall")

        # The reply must actually use the recalled location, both at the
        # tool call layer and in the user-facing reply.
        time_calls = [c for c in capture.calls if c["name"] == "getTime"]
        manchester_calls = [
            c for c in time_calls
            if "manchester" in (c["args"].get("location") or "").lower()
        ]
        assert manchester_calls, (
            "getTime was not invoked with location='Manchester' even "
            "though the diary contains the user's stated location. The "
            "memory enrichment → tool argument grounding path is broken. "
            f"All getTime calls: {[c['args'] for c in time_calls]}. "
            f"Tools observed: {capture.tool_names()}. "
            f"Response: {(response or '')[:400]}"
        )

        response_lower = (response or "").lower()
        assert "manchester" in response_lower, (
            "Reply does not mention Manchester despite the diary stating "
            f"the user lives there. Response: {(response or '')[:400]}"
        )

        # Guard against a hardcoded-default leak: any reply that mentions
        # Hackney here is wrong (Hackney is the test fixture's geoip
        # default, but geoip is disabled in this test).
        assert "hackney" not in response_lower, (
            "Reply mentions Hackney — the diary clearly states Manchester, "
            "and geoip is disabled in this test. The model leaked a "
            f"hardcoded default. Response: {(response or '')[:400]}"
        )
