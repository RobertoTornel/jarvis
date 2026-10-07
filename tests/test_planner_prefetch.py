"""The speculative planner prefetch, and the engine wiring that uses it.

The planner and the tool router are serial LLM round-trips at the top of the
reply flow. `planner_prefetch` starts the planner on a background thread
before the router, planning against the FULL catalogue because the router's
narrowing does not exist yet. That is only acceptable if three things hold,
and these tests pin each of them:

1. It is genuinely used — the plan the engine follows comes from the
   speculative full-catalogue call, and the serial planner is not run again
   on top of it.
2. It is never slower — the wait is bounded by the same wall-clock the serial
   ordering gives the planner (router elapsed + one planner ceiling), and a
   prefetch that is still in flight at that deadline fails open instead of
   buying a second full ceiling.
3. It never loses a plan the serial path would have produced — a prefetch
   that settles *without* a plan is retried on the serial, narrowed-catalogue
   path, and a speculative plan that names a tool the router did not pick
   still stays in the allow-list.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from src.claire.reply.planner_prefetch import (
    PlannerPrefetch,
    start_planner_prefetch,
)

pytestmark = pytest.mark.unit

# ── the handle ───────────────────────────────────────────────────────────

def _settle(h, within=5.0):
    """Wait for the worker without using the handle's own API to do it."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if h.settled:
            return
        time.sleep(0.002)
    raise AssertionError("the prefetch worker never settled")


def test_plan_if_ready_is_none_until_the_worker_finishes_then_the_plan():
    release = threading.Event()
    h = start_planner_prefetch(
        call=lambda: (release.wait(5.0), ["getTime location='Tokyo'"])[1],
        timeout_sec=5.0,
    )
    # Held open, so the "not yet" state is deterministic rather than a race.
    assert h.settled is False
    assert h.plan_if_ready() is None
    release.set()
    _settle(h)
    assert h.settled is True
    assert h.plan_if_ready() == ["getTime location='Tokyo'"]


def test_plan_if_ready_never_blocks():
    """The whole safety property in one test: an unfinished worker must cost
    the caller nothing. This is what lets the engine treat 'no plan in hand' as
    'run the planner myself' rather than as a deadline to wait out."""
    release = threading.Event()
    h = start_planner_prefetch(
        call=lambda: (release.wait(5.0), ["x"])[1], timeout_sec=0.05
    )
    try:
        t0 = time.monotonic()
        for _ in range(1000):
            assert h.plan_if_ready() is None
        assert time.monotonic() - t0 < 0.5, "plan_if_ready blocked"
    finally:
        release.set()


def test_a_failing_call_is_a_settled_no_plan_and_is_reported():
    seen = []
    h = PlannerPrefetch(
        call=lambda: (_ for _ in ()).throw(RuntimeError("backend down")),
        timeout_sec=5.0,
        on_error=seen.append,
    ).start()
    _settle(h)
    assert h.plan_if_ready() is None
    assert len(seen) == 1 and isinstance(seen[0], RuntimeError)


def test_a_non_list_result_is_treated_as_no_plan():
    """A stray scalar from a confused backend must not become a plan."""
    h = start_planner_prefetch(call=lambda: "getTime location='Tokyo'", timeout_sec=5.0)
    _settle(h)
    assert h.plan_if_ready() is None


def test_an_empty_plan_is_reported_as_empty_not_as_a_plan():
    """`[]` is the planner's "nothing to do", and it stays `[]`.

    The handle does not launder it into a plan or into a failure: the caller
    decides. The engine's rule is that only a *non-empty* plan is worth
    following, so `[]` sends the turn down the serial path — which is strictly
    better than following an empty plan, and identical to the pre-prefetch
    behaviour when the serial planner also returns `[]`.
    """
    h = start_planner_prefetch(call=lambda: [], timeout_sec=5.0)
    _settle(h)
    assert h.plan_if_ready() == []
    assert h.plan_if_ready() is not None


def test_start_never_raises_when_the_thread_cannot_start():
    with patch("src.claire.reply.planner_prefetch.threading.Thread") as th:
        th.return_value.start.side_effect = RuntimeError("no threads left")
        h = start_planner_prefetch(call=lambda: ["x"], timeout_sec=5.0)
    assert h.settled is True
    assert h.plan_if_ready() is None


def test_the_call_runs_off_the_calling_thread():
    names = []
    h = PlannerPrefetch(
        call=lambda: names.append(threading.current_thread().name) or ["x"],
        timeout_sec=5.0,
    ).start()
    _settle(h)
    assert names == ["planner-prefetch"]
    assert threading.current_thread().name != "planner-prefetch"


# ── engine wiring ────────────────────────────────────────────────────────

_SYSTEM_MESSAGE = {"role": "system", "content": ""}


def _drive_engine(mock_config, db, dialogue_memory, query, plan_query_fake,
                  select_tools_ret=None, extractor=None, chat=None,
                  router_delay=0.0):
    """Run one turn, returning (captured system messages, tool schemas).

    ``router_delay`` makes the turn's router slow on purpose. The engine now
    takes a speculative plan only if it is *already* in hand when the router
    finishes, so any test that wants the prefetch to be consumed has to give
    the worker a real head start — otherwise the test would be racing the
    thread rather than pinning the contract.
    """
    from src.claire.reply import engine as engine_mod

    mock_config.llm_chat_model = "gpt-oss:20b"  # LARGE: plan goes in the prompt
    mock_config.evaluator_enabled = False

    captured = {"systems": [], "tools": []}

    def fake_select_tools(*a, **k):
        if router_delay:
            time.sleep(router_delay)
        return ["schedule", "stop"] if select_tools_ret is None else list(select_tools_ret)

    def fake_chat(*args, **kwargs):
        msgs = kwargs.get("messages") or (args[2] if len(args) > 2 else [])
        for m in msgs:
            if m.get("role") == "system":
                captured["systems"].append(m.get("content", ""))
                break
        captured["tools"].append(
            [s["function"]["name"] for s in (kwargs.get("tools") or [])]
        )
        return {"message": {"role": "assistant", "content": "All done."}}

    patches = [
        patch.object(engine_mod, "chat_with_messages", side_effect=chat or fake_chat),
        patch.object(engine_mod, "plan_query", side_effect=plan_query_fake),
        patch.object(engine_mod, "select_tools", side_effect=fake_select_tools),
        patch.object(
            engine_mod,
            "extract_search_params_for_memory",
            side_effect=extractor or (lambda *a, **k: {"keywords": []}),
        ),
    ]
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        engine_mod.run_reply_engine(
            db=db, cfg=mock_config, tts=None, text=query, dialogue_memory=dialogue_memory
        )
    return captured


def _full_catalogue_sizes():
    from src.claire.tools.registry import BUILTIN_TOOLS

    return len(BUILTIN_TOOLS)


def test_engine_consumes_the_speculative_full_catalogue_plan(mock_config, db, dialogue_memory):
    """The plan the turn follows must come from the prefetch, not a serial run.

    This is the wiring proof. The serial path can only ever hand the planner
    the router's narrowed catalogue (`schedule`, `stop`); the speculative path
    hands it the full one. So a plan that names `getTime` — a tool the router
    did not pick — can only have come from the prefetch. Remove the call site
    and this fails: the serial call would receive the narrowed catalogue and
    return the reply-only plan.
    """
    calls = []

    def fake_plan_query(*args, **kwargs):
        names = [n for n, _ in kwargs["tools"]]
        calls.append(names)
        if "getTime" in names:
            return ["getTime location='Tokyo'", "Reply to the user with the time."]
        return ["Reply to the user."]

    captured = _drive_engine(
        mock_config, db, dialogue_memory,
        "what time is it in Tokyo right now please tell me exactly",
        fake_plan_query,
        router_delay=0.05,
    )

    # Exactly one planner call, and it saw the full catalogue.
    assert len(calls) == 1, f"planner ran {len(calls)}x — the prefetch was not consumed"
    assert len(calls[0]) == _full_catalogue_sizes()
    assert "getTime" in calls[0]
    # The consumed plan is the speculative one, and its tool stays executable
    # even though the router never picked it.
    assert captured["tools"] and "getTime" in captured["tools"][0]
    assert any("getTime location='Tokyo'" in s for s in captured["systems"])


def test_the_speculative_plan_tool_outside_the_router_picks_stays_in_the_allow_list(
    mock_config, db, dialogue_memory
):
    def fake_plan_query(*args, **kwargs):
        if "getTime" in [n for n, _ in kwargs["tools"]]:
            return ["getTime location='Lisbon'", "Reply to the user."]
        return ["schedule action='list'"]

    captured = _drive_engine(
        mock_config, db, dialogue_memory,
        "when is it where I am flying to next week please",
        fake_plan_query, select_tools_ret=["schedule", "stop"],
    )
    allowed = captured["tools"][0]
    assert "getTime" in allowed, "plan-named tool must be unioned into the allow-list"
    assert "schedule" in allowed, "the router's own pick must survive"


def test_a_settled_prefetch_without_a_plan_falls_back_to_the_serial_planner(
    mock_config, db, dialogue_memory
):
    """A cheap failure must not cost the turn its plan."""
    calls = []

    def fake_plan_query(*args, **kwargs):
        names = [n for n, _ in kwargs["tools"]]
        calls.append(names)
        return ["schedule action='list'", "Reply to the user."]

    class SettledNoPlan:
        settled = True

        def plan_if_ready(self):
            return None

        def wait(self, within):  # pragma: no cover - must never be called
            raise AssertionError("the engine must not wait on the prefetch")

    from src.claire.reply import engine as engine_mod
    with patch.object(engine_mod, "start_planner_prefetch", return_value=SettledNoPlan()):
        captured = _drive_engine(
            mock_config, db, dialogue_memory,
            "what is on my calendar for the rest of this week",
            fake_plan_query,
        )

    assert len(calls) == 1 and calls[0] == ["schedule", "stop"], (
        "the fallback must use the router-narrowed catalogue, exactly like the "
        "serial path did before the prefetch existed"
    )
    assert any("schedule action='list'" in s for s in captured["systems"])


def test_an_in_flight_prefetch_still_gets_the_serial_planner(
    mock_config, db, dialogue_memory
):
    """The engine must not decide a plan is unnecessary just because the
    speculative one is late.

    This is the counterexample that killed the waiting design: on a backend
    that queues the planner behind the router, the speculative call can still
    be running when the router finishes. Waiting for it spent the turn's whole
    budget and then followed no plan at all — strictly slower than serial, and
    planless where serial had a plan. Requiring the plan to be in hand makes
    that case fall through to the serial planner, which is exactly what the
    turn would have done without the prefetch.
    """
    calls = []

    def fake_plan_query(*args, **kwargs):
        calls.append([n for n, _ in kwargs["tools"]])
        return ["schedule action='list'", "Reply to the user with the combined findings."]

    class StillRunning:
        """A worker that never settles — the worst case for a waiting design."""

        settled = False

        def plan_if_ready(self):
            return None

        def wait(self, within):  # pragma: no cover - must never be called
            raise AssertionError("the engine must not wait on the prefetch")

    from src.claire.reply import engine as engine_mod
    with patch.object(engine_mod, "start_planner_prefetch", return_value=StillRunning()):
        captured = _drive_engine(
            mock_config, db, dialogue_memory,
            "what did we decide about the kitchen renovation project",
            fake_plan_query,
        )

    assert calls == [["schedule", "stop"]], (
        "an in-flight prefetch must fall back to the serial planner with the "
        f"router-narrowed catalogue; plan_query saw {calls}"
    )
    assert any("schedule action='list'" in m for m in captured["systems"])


def test_a_late_speculative_plan_costs_the_turn_nothing(
    mock_config, db, dialogue_memory
):
    """The whole point of never waiting: a worker that is slow, or that never
    answers at all, must leave the turn's latency and its plan untouched."""
    class NeverAnswers:
        settled = False

        def plan_if_ready(self):
            return None

        def wait(self, within):  # pragma: no cover - must never be called
            raise AssertionError("the engine must not wait on the prefetch")

    from src.claire.reply import engine as engine_mod
    with patch.object(engine_mod, "start_planner_prefetch", return_value=NeverAnswers()):
        t0 = time.monotonic()
        captured = _drive_engine(
            mock_config, db, dialogue_memory,
            "what is on my calendar for the rest of this week",
            lambda *a, **k: ["schedule action='list'", "Reply to the user."],
        )
        elapsed = time.monotonic() - t0

    assert any("schedule action='list'" in m for m in captured["systems"])
    # No deadline arithmetic to spend: the turn is the router plus one planner,
    # so it cannot have waited out a second planner ceiling.
    assert elapsed < float(getattr(mock_config, "planner_timeout_sec", 3.0)) + 2.0, (
        f"a turn with an unanswered prefetch took {elapsed:.2f}s — it waited"
    )


def test_the_speculative_call_is_bounded_by_the_planner_ceiling(
    mock_config, db, dialogue_memory
):
    """The abandoned worker's own ceiling stays the planner ceiling.

    Now that the engine never waits, a longer speculative ceiling would buy
    nothing — a plan is only ever taken if it is ready before the router
    returns — while a hung backend would hold a daemon thread for that much
    longer. The ceiling is therefore deliberately NOT `planner + router`.
    """
    mock_config.planner_timeout_sec = 0.4
    mock_config.llm_tools_timeout_sec = 6.0
    seen = []

    def fake_plan_query(*args, **kwargs):
        seen.append(kwargs)
        return ["Reply to the user."]

    _drive_engine(
        mock_config, db, dialogue_memory,
        "what is on my calendar for the rest of this week",
        fake_plan_query,
    )
    assert seen, "the prefetch must have called the planner"
    assert seen[0]["timeout_sec"] == mock_config.planner_timeout_sec


def test_the_planner_is_not_started_when_it_is_disabled(mock_config, db, dialogue_memory):
    mock_config.planner_enabled = False
    started = []

    from src.claire.reply import engine as engine_mod
    with patch.object(
        engine_mod, "start_planner_prefetch",
        side_effect=lambda **k: started.append(k) or None,
    ):
        _drive_engine(
            mock_config, db, dialogue_memory,
            "what is on my calendar for the rest of this week",
            lambda *a, **k: ["Reply to the user."],
        )
    assert started == []


def test_the_planner_is_not_started_for_a_query_too_short_to_plan(
    mock_config, db, dialogue_memory
):
    """`plan_query` would reject it anyway; do not spend a thread on it."""
    started = []

    from src.claire.reply import engine as engine_mod
    with patch.object(
        engine_mod, "start_planner_prefetch",
        side_effect=lambda **k: started.append(k) or None,
    ):
        _drive_engine(
            mock_config, db, dialogue_memory, "ok",
            lambda *a, **k: ["Reply to the user."],
        )
    assert started == []


def test_a_short_tool_free_turn_is_not_held_open_by_the_prefetch(
    mock_config, db, dialogue_memory
):
    """The fast path must never wait on the speculative planner.

    The prefetch still fires (the skip decision needs the router), so what is
    pinned here is that the turn does not *consume* it: the reply-only plan is
    used and the turn finishes without blocking on the planner's ceiling.
    """
    release = threading.Event()

    def slow_plan_query(*args, **kwargs):
        release.wait(10.0)
        return ["getTime location='Nowhere'", "Reply to the user."]

    mock_config.planner_timeout_sec = 8.0
    t0 = time.perf_counter()
    try:
        captured = _drive_engine(
            mock_config, db, dialogue_memory, "hello there", slow_plan_query,
            select_tools_ret=["stop"],
        )
    finally:
        release.set()
    elapsed = time.perf_counter() - t0

    assert elapsed < 5.0, "a skipped turn must not wait on the prefetch"
    # Reply-only plan: the speculative tool step never reached the prompt.
    assert not any("getTime location='Nowhere'" in s for s in captured["systems"])
