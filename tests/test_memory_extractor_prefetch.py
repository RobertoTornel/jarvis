"""Unit coverage for the speculative keyword-memory-extractor prefetch.

Finding E: the extractor depends only on the redacted query and the
enrichment context hint, both known before the tool router and planner run,
so it is started on a background thread and published into the
conversation-scoped hot-window cache the serial path reads.

The correctness bar here is what matters: a speculative result must never be
read before it is ready, never be attributed to a query or topic hint it was
not computed for, and never leak across a conversation reset. These tests
pin each of those, plus the "abandonment is free" contract that lets the
engine fire the prefetch on turns that turn out not to need memory.
"""

import threading
import time

import pytest

from src.claire.memory.conversation import DialogueMemory
from src.claire.reply.memory_prefetch import (
    MemoryExtractorPrefetch,
    start_memory_extractor_prefetch,
)

pytestmark = pytest.mark.unit


def _dm() -> DialogueMemory:
    return DialogueMemory(inactivity_timeout=300, max_interactions=20)


# ── start gating ─────────────────────────────────────────────────────────


def test_no_prefetch_without_a_dialogue_memory():
    assert start_memory_extractor_prefetch(
        query="q", call=lambda: {"keywords": ["x"]}, dialogue_memory=None
    ) is None


def test_no_prefetch_without_a_usable_hot_cache():
    class Bare:
        pass

    assert start_memory_extractor_prefetch(
        query="q", call=lambda: {"keywords": ["x"]}, dialogue_memory=Bare()
    ) is None


def test_no_prefetch_when_the_key_is_already_cached():
    """A second speculative call for a cached answer is pure waste."""
    dm = _dm()
    dm.hot_cache_put("enrichment:what did I say about the dentist", {"keywords": ["dentist"]})
    calls = []
    handle = start_memory_extractor_prefetch(
        query="what did I say about the dentist",
        call=lambda: calls.append(1) or {"keywords": ["x"]},
        dialogue_memory=dm,
    )
    assert handle is None
    assert calls == []


# ── publish / read ───────────────────────────────────────────────────────


def test_result_is_published_under_the_serial_cache_key():
    dm = _dm()
    handle = start_memory_extractor_prefetch(
        query="what did I say about the dentist",
        call=lambda: {"keywords": ["dentist", "appointment"]},
        dialogue_memory=dm,
    )
    assert handle is not None
    assert handle.result(5.0) == {"keywords": ["dentist", "appointment"]}
    # The serial path reads exactly this key for a topic-free query.
    assert dm.hot_cache_get("enrichment:what did I say about the dentist") == {
        "keywords": ["dentist", "appointment"]
    }


def test_result_returns_none_after_the_budget_without_waiting_forever():
    release = threading.Event()
    dm = _dm()
    handle = start_memory_extractor_prefetch(
        query="q", call=lambda: (release.wait(10), {"keywords": ["x"]})[1],
        dialogue_memory=dm,
    )
    t0 = time.monotonic()
    assert handle.result(0.05) is None
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"result() must honour its budget; waited {elapsed:.3f}s"
    assert dm.hot_cache_get("enrichment:q") is None
    release.set()


def test_wait_is_clamped_to_the_remaining_budget():
    """A prefetch already in flight must not get a *fresh* full budget."""
    release = threading.Event()
    dm = _dm()
    handle = start_memory_extractor_prefetch(
        query="q", call=lambda: (release.wait(10), {"keywords": ["x"]})[1],
        dialogue_memory=dm,
    )
    time.sleep(0.15)
    t0 = time.monotonic()
    assert handle.result(0.2) is None
    elapsed = time.monotonic() - t0
    # 0.2s budget minus ~0.15s already spent leaves ~0.05s, not 0.2s.
    assert elapsed < 0.15, f"wait was not clamped to the remaining budget ({elapsed:.3f}s)"
    release.set()


# ── failure containment ──────────────────────────────────────────────────


def test_a_raising_call_is_swallowed_and_nothing_is_published():
    dm = _dm()

    def boom():
        raise RuntimeError("backend exploded")

    handle = MemoryExtractorPrefetch(
        cache_key="enrichment:q", call=boom, dialogue_memory=dm
    ).start()
    assert handle.result(2.0) is None
    assert dm.hot_cache_get("enrichment:q") is None
    assert handle.done


def test_a_non_dict_result_is_not_published():
    dm = _dm()
    handle = start_memory_extractor_prefetch(
        query="q", call=lambda: "not a dict", dialogue_memory=dm
    )
    assert handle.result(2.0) is None
    assert dm.hot_cache_get("enrichment:q") is None


def test_empty_dict_is_a_valid_result_and_is_published():
    """``{}`` is the extractor's documented failure value; the serial path
    caches it too, so the prefetch must not diverge from that contract."""
    dm = _dm()
    handle = start_memory_extractor_prefetch(query="q", call=lambda: {}, dialogue_memory=dm)
    assert handle.result(2.0) == {}
    assert dm.hot_cache_get("enrichment:q") == {}


# ── conversation isolation ───────────────────────────────────────────────


def test_a_prefetch_does_not_publish_across_a_cache_wipe():
    """The whole point of the epoch guard: an abandoned prefetch started in
    conversation A must not land in conversation B's cache under the same
    query key, where it would be read as a memory answer computed for a
    different context hint."""
    release = threading.Event()
    dm = _dm()
    handle = start_memory_extractor_prefetch(
        query="same words",
        call=lambda: (release.wait(10), {"keywords": ["stale"]})[1],
        dialogue_memory=dm,
    )

    # A new conversation begins while the speculative call is still in flight.
    dm.clear_hot_cache()
    assert dm.hot_cache_epoch == 1

    release.set()
    assert handle.result(2.0) == {"keywords": ["stale"]}
    assert dm.hot_cache_get("enrichment:same words") is None, (
        "a prefetch from the previous conversation must not repopulate the cache"
    )


def test_stale_epoch_does_not_block_a_current_publish():
    dm = _dm()
    dm.clear_hot_cache()
    handle = start_memory_extractor_prefetch(
        query="q", call=lambda: {"keywords": ["fresh"]}, dialogue_memory=dm
    )
    assert handle.result(2.0) == {"keywords": ["fresh"]}
    assert dm.hot_cache_get("enrichment:q") == {"keywords": ["fresh"]}


def test_hot_cache_epoch_only_advances_on_a_wipe():
    dm = _dm()
    start = dm.hot_cache_epoch
    dm.hot_cache_put("k", {"v": 1})
    dm.hot_cache_get("k")
    assert dm.hot_cache_epoch == start
    dm.invalidate_warm_profile()
    assert dm.hot_cache_epoch == start
    dm.clear_hot_cache()
    assert dm.hot_cache_epoch == start + 1


# ── engine integration: the prefetch must be genuinely consumed ───────────


from unittest.mock import Mock, patch  # noqa: E402


def _mock_cfg():
    cfg = Mock()
    cfg.ollama_base_url = "http://localhost:11434"
    cfg.ollama_chat_model = "test-large"
    cfg.llm_chat_model = "test-large"
    cfg.voice_debug = False
    cfg.llm_tools_timeout_sec = 8.0
    cfg.llm_embedding_timeout_sec = 10.0
    cfg.llm_chat_timeout_sec = 45.0
    cfg.llm_digest_timeout_sec = 8.0
    cfg.memory_enrichment_max_results = 5
    cfg.memory_enrichment_source = "diary"
    cfg.memory_digest_enabled = False
    cfg.tool_result_digest_enabled = False
    cfg.location_ip_address = None
    cfg.location_auto_detect = False
    cfg.location_enabled = False
    cfg.agentic_max_turns = 8
    cfg.tool_search_max_calls = 3
    cfg.tool_selection_strategy = "all"
    cfg.tool_carryover_max_turns = 2
    cfg.tool_carryover_per_entry_chars = 1200
    cfg.mcps = {}
    cfg.llm_thinking_enabled = False
    cfg.tts_engine = "none"
    cfg.ollama_embed_model = "test-embed"
    cfg.db_path = ":memory:"
    return cfg


_ENGINE_PATCHES = [
    patch("src.claire.memory.graph_ops.format_warm_profile_block", return_value=""),
    patch("src.claire.memory.graph_ops.build_warm_profile",
          return_value={"user": "", "directives": ""}),
    patch("src.claire.memory.graph.GraphMemoryStore"),
    patch("src.claire.reply.engine.select_tools", return_value=["webSearch"]),
    patch("src.claire.memory.conversation.search_conversation_memory_by_keywords",
          return_value=[]),
    patch("src.claire.reply.engine.extract_text_from_response"),
    patch("src.claire.reply.engine.chat_with_messages"),
]


def _run_with(plan, extractor_mock, text="what did I say about the dentist"):
    from src.claire.reply.engine import run_reply_engine

    with patch("src.claire.reply.engine.plan_query", return_value=plan), \
         patch("src.claire.reply.engine.extract_search_params_for_memory",
               extractor_mock):
        ctx = []
        for p in _ENGINE_PATCHES:
            ctx.append(p)
        mocks = [p.start() for p in ctx]
        try:
            mocks[-2].return_value = "ok"          # extract_text_from_response
            mocks[-1].return_value = {"message": {"content": "ok"}}  # chat
            run_reply_engine(db=Mock(), cfg=_mock_cfg(), tts=None,
                             text=text, dialogue_memory=DialogueMemory())
        finally:
            for p in reversed(ctx):
                p.stop()
    return mocks


@pytest.mark.unit
def test_topic_free_memory_turn_is_served_by_the_speculative_prefetch():
    """The whole point of Finding E: when the planner asks for memory with no
    topic hint, the engine must find the answer already computed and must NOT
    pay a second, serial extractor round-trip.

    Provenance matters more than the count here: exactly one call happens
    whether or not the prefetch works, so the assertion is that the call ran
    on the prefetch worker thread. A call on the main thread would mean the
    speculative answer was ignored and the serial path ran instead.
    """
    seen = []

    def fake_extract(query, *a, **kwargs):
        seen.append((query, threading.current_thread().name))
        return {"keywords": ["dentist"]}

    _run_with(["searchMemory", "reply"], fake_extract)

    assert len(seen) == 1, (
        "a topic-free memory turn must be served by exactly one extractor "
        f"call; got {seen!r}"
    )
    assert seen[0][0] == "what did I say about the dentist"
    assert seen[0][1] == "memory-extractor-prefetch", (
        "the extractor call must have been made by the speculative prefetch, "
        f"not serially on the turn's own thread; got thread {seen[0][1]!r}"
    )


@pytest.mark.unit
def test_topic_hinted_memory_turn_still_runs_its_own_topic_call():
    """A planner topic hint changes the extractor's input, so the no-topic
    speculative answer must never be substituted for it."""
    calls = []

    def fake_extract(query, *a, **kwargs):
        calls.append((query, threading.current_thread().name))
        return {"keywords": ["dentist"]}

    _run_with(["searchMemory topic='dentist appointment'", "reply"], fake_extract)

    topic_calls = [c for c in calls if "[Memory topic: dentist appointment]" in c[0]]
    assert len(topic_calls) == 1, (
        "the topic-hinted extractor call must still run exactly once; got "
        f"{calls!r}"
    )
    assert topic_calls[0][1] == "MainThread", (
        "the topic-hinted call is the turn's own serial call; it must not be "
        f"served from the no-topic prefetch; got thread {topic_calls[0][1]!r}"
    )
    # The speculative call, if it ran, used the raw query with no topic.
    for query, _thread in calls:
        assert query != topic_calls[0][0] or "[Memory topic:" in query


@pytest.mark.unit
def test_repeat_topic_free_turn_does_not_restart_the_prefetch():
    """Once the answer is cached, the next identical turn must not start a
    second speculative call — the prefetch's own start gate reads the cache."""
    dm = DialogueMemory()
    mock_extractor = Mock(return_value={"keywords": ["dentist"]})

    from src.claire.reply.engine import run_reply_engine

    with patch("src.claire.reply.engine.plan_query", return_value=["searchMemory", "reply"]), \
         patch("src.claire.reply.engine.extract_search_params_for_memory", mock_extractor):
        ctx = [patch("src.claire.memory.graph_ops.format_warm_profile_block", return_value=""),
               patch("src.claire.memory.graph_ops.build_warm_profile",
                     return_value={"user": "", "directives": ""}),
               patch("src.claire.memory.graph.GraphMemoryStore"),
               patch("src.claire.reply.engine.select_tools", return_value=["webSearch"]),
               patch("src.claire.reply.engine.extract_text_from_response"),
               patch("src.claire.reply.engine.chat_with_messages")]
        mocks = [p.start() for p in ctx]
        try:
            mocks[-2].return_value = "ok"
            mocks[-1].return_value = {"message": {"content": "ok"}}
            for _ in range(2):
                run_reply_engine(db=Mock(), cfg=_mock_cfg(), tts=None,
                                 text="what did I say about the dentist",
                                 dialogue_memory=dm)
        finally:
            for p in reversed(ctx):
                p.stop()

    assert mock_extractor.call_count == 1, (
        "the second identical turn must be served from the hot-window cache; "
        f"got {mock_extractor.call_count} calls"
    )
