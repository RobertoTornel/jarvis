"""Warm-profile location facts ground time tool arguments and answers.

GeoIP is disabled. The User branch supplies Edinburgh without an explicit
memory-search step. Model inference uses the selected evaluation transport.
"""

from unittest.mock import patch

import pytest

from evals.memory_tool_grounding import assert_time_reply

from conftest import requires_judge_llm
from helpers import (
    ToolCallCapture,
    JUDGE_MODEL,
    voice_config,
)


_EDINBURGH_TIME = """Current time in Edinburgh, United Kingdom:
Timezone: Europe/London
Local time: 11:05
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
                        "Timezone: Europe/London\n"
                        "Local time: 11:05\n"
                    ),
                )
            if "edinburgh" not in location.casefold():
                return ToolExecutionResult(
                    success=False,
                    reply_text="This fixture has no time for that location.",
                )
            return ToolExecutionResult(
                success=True,
                reply_text=_EDINBURGH_TIME,
            )
        return ToolExecutionResult(success=True, reply_text="OK")

    return _runner

@pytest.mark.eval
@requires_judge_llm
class TestGraphSuppliesMissingToolArg:
    """Warm-profile injection path: a User-branch fact ("lives in
    Edinburgh") is always loaded into the system prompt, so the chat
    model can supply it as the location argument without an extra
    memory search."""

    def test_warm_profile_user_fact_grounds_time_call(
        self, eval_db, eval_dialogue_memory,
    ):
        from claire.reply.engine import run_reply_engine

        cfg = voice_config()
        # Geoip disabled — the only way the model gets a location is from
        # the warm profile loaded out of the graph.
        cfg.location_enabled = False

        capture = ToolCallCapture()

        # Inject a User-branch fact directly into the warm-profile builder
        # rather than seeding the SQLite-backed graph store. The warm-
        # profile path the engine relies on is `build_warm_profile` →
        # `format_warm_profile_block`; seeding via the public API replays
        # the production shape without depending on graph-mutation
        # listeners or branch-root bootstrapping in the test DB.
        warm_profile = {
            "user": "The user lives in Edinburgh.",
            "directives": "",
        }

        with patch(
            "claire.memory.graph_ops.build_warm_profile",
            return_value=warm_profile,
        ), patch(
            "claire.reply.engine.run_tool_with_retries",
            side_effect=_make_runner(capture),
        ):
            response = run_reply_engine(
                db=eval_db, cfg=cfg, tts=None,
                text="what time is it, Claire?",
                dialogue_memory=eval_dialogue_memory,
            )

        print(f"\n  🧠 Warm Profile Supplies Missing Tool Arg ({JUDGE_MODEL}):")
        print(f"  🛠️ Tools called: {capture.tool_names()}")
        for c in capture.calls:
            print(f"    🔧 {c['name']}({c['args']})")
        print(f"  💬 Response: {(response or '')[:300]}")

        assert_time_reply(response, _EDINBURGH_TIME, "warm-profile")

        # The reply must actually use the warm-profile location, both at
        # the tool call layer and in the user-facing reply.
        time_calls = [c for c in capture.calls if c["name"] == "getTime"]
        edinburgh_calls = [
            c for c in time_calls
            if "edinburgh" in (c["args"].get("location") or "").lower()
        ]
        assert edinburgh_calls, (
            "getTime was not invoked with location='Edinburgh' even "
            "though the warm profile states the user lives there. The "
            "warm-profile → tool argument grounding path is broken. "
            f"All getTime calls: {[c['args'] for c in time_calls]}. "
            f"Tools observed: {capture.tool_names()}. "
            f"Response: {(response or '')[:400]}"
        )

        response_lower = (response or "").lower()
        assert "edinburgh" in response_lower, (
            "Reply does not mention Edinburgh despite the warm profile "
            f"stating the user lives there. Response: {(response or '')[:400]}"
        )

        # Hackney is the test fixture's geoip default; geoip is disabled
        # here, so any reply mentioning it leaked a hardcoded default.
        assert "hackney" not in response_lower, (
            "Reply mentions Hackney — the warm profile states Edinburgh "
            "and geoip is disabled in this test. The model leaked a "
            f"hardcoded default. Response: {(response or '')[:400]}"
        )
