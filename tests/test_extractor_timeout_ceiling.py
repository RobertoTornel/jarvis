"""The memory keyword extractor gets its own, short LLM ceiling.

The extractor is a cheap classification-shaped call on the fast tier, but it
used to inherit ``llm_tools_timeout_sec`` — a ceiling sized for *tool
execution*, 15 s in the shipped config. A single unresponsive extractor call
therefore held the whole pre-chat phase (router -> planner -> extractor) open
for the full 15 s and then returned an empty ``{}`` anyway, so the tail bought
nothing. ``llm_extractor_timeout_sec`` bounds that tail on both extractor call
sites: the speculative prefetch and the serial fallback.

These tests pin:
  * the shipped default is present, positive and strictly shorter than the
    tool-execution ceiling it used to borrow;
  * the engine passes the extractor ceiling — not the tools ceiling — to the
    extractor, on the prefetch path;
  * a hand-built ``cfg`` that predates the key still runs (falling back to the
    old key) instead of raising.
"""

from unittest.mock import Mock, patch

import pytest

from claire.config import get_default_config, load_settings

pytestmark = pytest.mark.unit


# ── the config surface ───────────────────────────────────────────────────


def test_default_extractor_ceiling_is_present_and_shorter_than_the_tools_ceiling():
    defaults = get_default_config()
    assert "llm_extractor_timeout_sec" in defaults
    extractor = defaults["llm_extractor_timeout_sec"]
    assert isinstance(extractor, (int, float))
    assert extractor > 0
    # The whole point: it must not inherit the tool-execution ceiling.
    assert extractor < defaults["llm_tools_timeout_sec"]


def test_extractor_ceiling_is_configurable(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"llm_extractor_timeout_sec": 3.5, "db_path": ":memory:"}')
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
    assert load_settings().llm_extractor_timeout_sec == 3.5


def test_extractor_ceiling_defaults_when_only_the_old_key_is_present(
    tmp_path, monkeypatch
):
    """A config file written before this key existed still loads."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"llm_tools_timeout_sec": 22.0, "db_path": ":memory:"}')
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
    cfg = load_settings()
    assert cfg.llm_tools_timeout_sec == 22.0
    assert cfg.llm_extractor_timeout_sec == get_default_config()[
        "llm_extractor_timeout_sec"
    ]


# ── the engine wiring ────────────────────────────────────────────────────


def _mock_cfg(*, with_new_key):
    from src.claire.memory.conversation import DialogueMemory  # noqa: F401

    cfg = Mock()
    cfg.ollama_base_url = "http://localhost:11434"
    cfg.ollama_chat_model = "test-large"
    cfg.llm_chat_model = "test-large"
    cfg.voice_debug = False
    cfg.llm_tools_timeout_sec = 15.0
    if with_new_key:
        cfg.llm_extractor_timeout_sec = 4.5
    # else: Mock() auto-creates the attribute as a Mock, which is exactly the
    # hand-built-cfg situation the isinstance fallback exists for.
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


def _capture_extractor_timeout(cfg, plan):
    """Drive one reply turn and return the timeout the extractor was given."""
    from src.claire.memory.conversation import DialogueMemory
    from src.claire.reply.engine import run_reply_engine

    seen = []
    extractor = Mock(side_effect=lambda *a, **kw: seen.append(kw) or {})

    with patch("src.claire.reply.engine.plan_query", return_value=plan), \
         patch("src.claire.reply.engine.extract_search_params_for_memory",
               extractor):
        ctx = [p.start() for p in _ENGINE_PATCHES]
        try:
            ctx[-2].return_value = "ok"
            ctx[-1].return_value = {"message": {"content": "ok"}}
            run_reply_engine(db=Mock(), cfg=cfg, tts=None,
                             text="what did I say about the dentist",
                             dialogue_memory=DialogueMemory())
        finally:
            for p in reversed(_ENGINE_PATCHES):
                p.stop()
    assert seen, "the extractor was never called"
    return seen[0].get("timeout_sec")


def test_engine_gives_the_extractor_its_own_ceiling():
    """The prefetch must carry the extractor ceiling, not the tools ceiling."""
    timeout = _capture_extractor_timeout(
        _mock_cfg(with_new_key=True), ["searchMemory topic='dentist'"]
    )
    assert timeout == 4.5


def test_engine_falls_back_to_the_tools_ceiling_without_the_new_key():
    """A cfg that predates the key keeps working, on the old ceiling."""
    timeout = _capture_extractor_timeout(
        _mock_cfg(with_new_key=False), ["searchMemory topic='dentist'"]
    )
    assert timeout == 15.0
