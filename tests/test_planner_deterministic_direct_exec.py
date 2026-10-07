"""Deterministic-only direct execution for LARGE models.

The engine's plan-driven direct-exec block used to be SMALL-only. It now
also runs for LARGE models, restricted to `resolve_next_tool_call`'s
deterministic fast path: a step the planner already spelled out is
dispatched without a chat turn, and a step that would have needed the LLM
resolver is left to the chat model's own native tool calling.

The point of these tests is that the LARGE path can never *add* an LLM
call — so the resolver indirection is patched to fail, and every test
still has to pass.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from claire.reply import planner as planner_mod

pytestmark = pytest.mark.unit

SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "getWeather",
            "description": "Weather data.",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "getTime",
            "description": "Current time.",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "localFiles",
            "description": "Read files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "operation": {"type": "string"},
                },
                "required": ["path", "operation"],
            },
        },
    },
]


def _config():
    from types import SimpleNamespace

    return SimpleNamespace(
        llm_chat_model="gpt-oss:20b",
        fast_model="",
        planner_timeout_sec=3.0,
    )


def test_deterministic_only_dispatches_a_concrete_step_without_any_llm():
    """The whole point: a fully concrete step costs zero inference."""
    with patch.object(
        planner_mod, "call_llm_direct",
        side_effect=AssertionError("deterministic_only must never call the LLM"),
    ):
        resolved = planner_mod.resolve_next_tool_call(
            cfg=_config(),
            next_step_text="getWeather location='Lisbon'",
            prior_results=[],
            tools_schema=SCHEMA,
            deterministic_only=True,
        )
    assert resolved == ("getWeather", {"location": "Lisbon"})


def test_deterministic_only_drops_a_qualifier_key_the_schema_does_not_carry():
    """A planner often adds a qualifier the tool does not declare.

    `getWeather` takes only `location`, but the planner emitted
    `timeframe='tomorrow'` as well. Because the step already names a
    declared argument, the extra key is a qualifier to discard — exactly
    what the LLM resolver does with it — so the free parse is kept.
    """
    with patch.object(
        planner_mod, "call_llm_direct",
        side_effect=AssertionError("deterministic_only must never call the LLM"),
    ):
        resolved = planner_mod.resolve_next_tool_call(
            cfg=_config(),
            next_step_text="getWeather location='Lisbon' timeframe='tomorrow'",
            prior_results=[],
            tools_schema=SCHEMA,
            deterministic_only=True,
        )
    assert resolved == ("getWeather", {"location": "Lisbon"})


def test_synonym_only_step_defers_instead_of_dispatching_empty_args():
    """Regression guard: `getTime city='Tokyo'` must NOT become `getTime {}`.

    The step names none of the tool's declared arguments, so the unknown
    key carries its entire intent. Only the resolver can remap `city` to
    `location`; discarding it would run the tool with no arguments and
    answer about the wrong place.
    """
    with patch.object(
        planner_mod, "call_llm_direct",
        side_effect=AssertionError("deterministic_only must never call the LLM"),
    ):
        resolved = planner_mod.resolve_next_tool_call(
            cfg=_config(),
            next_step_text="getTime city='Tokyo'",
            prior_results=[],
            tools_schema=SCHEMA,
            deterministic_only=True,
        )
    assert resolved is None, (
        "a step that names none of the tool's arguments must defer to the "
        "chat model's native tool calling, never dispatch empty arguments"
    )


def test_dropping_an_unknown_key_cannot_lose_a_required_argument():
    """`localFiles` needs `operation`; `file_operation` is not that name."""
    with patch.object(
        planner_mod, "call_llm_direct",
        side_effect=AssertionError("deterministic_only must never call the LLM"),
    ):
        resolved = planner_mod.resolve_next_tool_call(
            cfg=_config(),
            next_step_text="localFiles file_operation='read' path='/tmp'",
            prior_results=[],
            tools_schema=SCHEMA,
            deterministic_only=True,
        )
    assert resolved is None


def test_deterministic_only_defers_an_under_specified_step():
    """A placeholder needs entity substitution, which is inference."""
    with patch.object(
        planner_mod, "call_llm_direct",
        side_effect=AssertionError("deterministic_only must never call the LLM"),
    ):
        resolved = planner_mod.resolve_next_tool_call(
            cfg=_config(),
            next_step_text="getWeather location='<city from step 1>'",
            prior_results=[],
            tools_schema=SCHEMA,
            deterministic_only=True,
        )
    assert resolved is None


def test_unknown_keys_still_defer_when_a_resolver_is_available():
    """SMALL-model behaviour is unchanged: unknown keys go to the resolver.

    The existing resolver path remaps names and drops unknown keys after
    inference, so it must keep receiving these steps.
    """
    sentinel = ("getWeather", {"location": "Lisbon"})
    with patch.object(
        planner_mod, "call_llm_direct", return_value='{"name": "getWeather", "arguments": {"location": "Lisbon"}}'
    ) as mocked:
        resolved = planner_mod.resolve_next_tool_call(
            cfg=_config(),
            next_step_text="getWeather city='Lisbon'",
            prior_results=[],
            tools_schema=SCHEMA,
        )
    assert mocked.called, "without deterministic_only the resolver must be consulted"
    assert resolved == sentinel


def test_engine_direct_execs_a_concrete_plan_step_on_a_large_model(
    mock_config, db, dialogue_memory
):
    """LARGE model + a concrete plan step → the tool runs without a chat turn.

    Two chat turns are expected in total (the plan has one tool step), not
    three: the tool call is dispatched from the plan, and only the final
    synthesis is left to the chat model.
    """
    from claire.reply import engine as engine_mod
    from claire.tools.types import ToolExecutionResult

    mock_config.llm_chat_model = "gpt-oss:20b"   # LARGE → native tools
    mock_config.ollama_chat_model = "gpt-oss:20b"
    mock_config.evaluator_enabled = False
    mock_config.tool_selection_strategy = "llm"

    plan = [
        "getWeather location='Lisbon' timeframe='tomorrow'",
        "Reply to the user with the combined findings.",
    ]
    chat_calls: list[list[dict]] = []
    invoked_tools: list[tuple[str, dict]] = []

    def fake_chat(*args, **kwargs):
        msgs = kwargs.get("messages") or (args[2] if len(args) > 2 else [])
        chat_calls.append(msgs)
        return {"message": {"role": "assistant", "content": "Tomorrow in Lisbon: grey."}}

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        invoked_tools.append((tool_name, tool_args))
        return ToolExecutionResult(success=True, reply_text="Partly cloudy, 23C.")

    with patch.object(engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
         patch.object(engine_mod, "select_tools", return_value=["getWeather", "stop"]), \
         patch.object(engine_mod, "plan_query", return_value=plan), \
         patch.object(engine_mod, "extract_search_params_for_memory", return_value={"keywords": []}), \
         patch.object(planner_mod, "call_llm_direct",
                      side_effect=AssertionError("the plan step is concrete; no resolver call")) :
        engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="What is the weather in Lisbon tomorrow?",
            dialogue_memory=dialogue_memory,
        )

    assert invoked_tools == [("getWeather", {"location": "Lisbon"})], (
        "the plan's concrete arguments must be dispatched verbatim"
    )
    assert len(chat_calls) == 1, (
        "exactly one chat turn is expected: the final synthesis. "
        f"got {len(chat_calls)}"
    )


def test_engine_leaves_a_synonym_plan_step_to_native_tool_calling(
    mock_config, db, dialogue_memory
):
    """LARGE + a step the fast path cannot parse → no plan-driven dispatch.

    The engine must not run the tool with the plan's unusable arguments;
    it falls through to the chat model, which is what happened before.
    """
    from claire.reply import engine as engine_mod
    from claire.tools.types import ToolExecutionResult

    mock_config.llm_chat_model = "gpt-oss:20b"
    mock_config.ollama_chat_model = "gpt-oss:20b"
    mock_config.evaluator_enabled = False
    mock_config.tool_selection_strategy = "llm"

    plan = ["getTime city='Tokyo'", "Reply to the user."]

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        raise AssertionError(
            "the engine must not direct-exec a step the deterministic parser refused"
        )

    with patch.object(engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner), \
         patch.object(engine_mod, "chat_with_messages",
                      return_value={"message": {"role": "assistant", "content": "It's late there."}}), \
         patch.object(engine_mod, "select_tools", return_value=["getTime", "stop"]), \
         patch.object(engine_mod, "plan_query", return_value=plan), \
         patch.object(engine_mod, "extract_search_params_for_memory", return_value={"keywords": []}), \
         patch.object(planner_mod, "call_llm_direct",
                      side_effect=AssertionError("LARGE must not pay the resolver LLM")):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="What time is it in Tokyo right now?",
            dialogue_memory=dialogue_memory,
        )

    assert reply, "the chat model must still produce the reply"
