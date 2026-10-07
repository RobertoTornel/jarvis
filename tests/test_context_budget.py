"""RED/GREEN tests for the context budget on what is SENT to the model.

The bug: ``DialogueMemory`` retains messages by TIME only (RECENT_WINDOW_SEC),
so a busy conversation packs an unbounded number of turns into the window and
the reply engine ships the whole history on every request. Measured on the
gateway: a 324-message conversation sent 133,893 input tokens; 99% of the wall
time was prefill. These tests pin a real ceiling on the history.

Invariants:
  * history sent per request is capped by message count AND estimated tokens;
  * the newest turns (and the system prompt) always survive in full;
  * a ``tool`` message is never orphaned from the assistant ``tool_calls``
    message that produced it (strict OpenAI servers 400 on that);
  * we never split a user/assistant pair so that history starts on a
    dangling assistant turn.
"""

from unittest.mock import Mock, patch

import pytest

from src.claire.memory.conversation import (
    DialogueMemory,
    bound_conversation_history,
    estimate_message_tokens,
)
from src.claire.reply.engine import run_reply_engine

pytestmark = pytest.mark.unit

# Defaults under test — must match the config defaults.
MAX_TOKENS = 8000
MAX_MESSAGES = 40


def _est_tokens(messages) -> int:
    """Local chars/4 estimate, independent of the implementation."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        total += len(content) + 16  # ~4 tokens fixed overhead per message
    return (total + 3) // 4


def _validator_ok(messages) -> None:
    """Assert OpenAI tool-message structure: no orphan tool results, no
    unanswered tool_calls, no leading dangling assistant turn."""
    pending = set()
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if isinstance(tc, dict) and tc.get("id"):
                    pending.add(tc["id"])
        elif role == "tool":
            assert m.get("tool_call_id") in pending, (
                f"orphaned tool message: tool_call_id={m.get('tool_call_id')!r} "
                f"not preceded by its assistant tool_calls"
            )
            pending.discard(m.get("tool_call_id"))
    assert not pending, f"unanswered tool_calls left dangling: {pending}"


def _fill(dm: DialogueMemory, turns: int, *, chars: int = 200) -> None:
    for i in range(turns):
        dm.add_interaction(
            f"user question number {i} " + ("x" * chars),
            f"assistant answer number {i} " + ("y" * chars),
        )


class TestDialogueMemoryContextBudget:
    def test_message_count_is_capped(self):
        """300 back-to-back turns must not all be handed to the model."""
        dm = DialogueMemory()
        _fill(dm, 300)
        out = dm.get_recent_turns_with_tools()
        assert len(out) <= MAX_MESSAGES, (
            f"unbounded history: {len(out)} messages returned from 300 turns"
        )

    def test_token_budget_is_enforced(self):
        """Long turns must be bounded by the estimated-token budget too."""
        dm = DialogueMemory()
        _fill(dm, 60, chars=8000)
        out = dm.get_recent_turns_with_tools()
        assert _est_tokens(out) <= MAX_TOKENS, (
            f"history is {_est_tokens(out)} estimated tokens "
            f"(cap {MAX_TOKENS})"
        )

    def test_get_recent_messages_stays_unbounded_for_ui_and_retrieval(self):
        """Deliberate: we bound what is SENT, not what is STORED. The raw
        getter still feeds the chat panel's transcript seed and diary
        retrieval, so it must keep returning the whole hot window."""
        dm = DialogueMemory()
        _fill(dm, 300)
        out = dm.get_recent_messages()
        assert len(out) == 600

    def test_newest_turns_survive_in_full(self):
        """Truncation is oldest-first: the newest pair must be intact."""
        dm = DialogueMemory()
        _fill(dm, 300)
        dm.add_interaction("NEWEST QUESTION", "NEWEST ANSWER")
        out = dm.get_recent_turns_with_tools()
        assert out[-2]["content"] == "NEWEST QUESTION"
        assert out[-1]["content"] == "NEWEST ANSWER"

    def test_history_does_not_start_on_dangling_assistant(self):
        dm = DialogueMemory()
        _fill(dm, 300)
        out = dm.get_recent_turns_with_tools()
        assert out, "expected some history to survive"
        assert out[0]["role"] == "user", (
            f"history starts on a dangling {out[0]['role']!r} turn"
        )

    def test_no_orphaned_tool_message_after_truncation(self):
        """A tool turn older than the text window must be dropped whole,
        never split from its assistant tool_calls parent."""
        dm = DialogueMemory()
        _fill(dm, 5)
        dm.record_tool_turn([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "webSearch",
                                 "arguments": {"query": "old thing"}},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1",
             "content": "old tool result " + ("z" * 500)},
        ])
        _fill(dm, 300)
        out = dm.get_recent_turns_with_tools(max_tool_turns=4)
        _validator_ok(out)
        # And the cap still holds with tool messages in the mix.
        assert len(out) <= MAX_MESSAGES + 4


class TestEngineContextBudget:
    def _cfg(self):
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
        cfg.dialogue_memory_max_tokens = MAX_TOKENS
        cfg.dialogue_memory_max_messages = MAX_MESSAGES
        return cfg

    def _snapshot_send(self, memory):
        """Run one reply turn and return a COPY of the messages the engine
        actually passed to the model. The copy matters: the engine keeps
        mutating that same list after the call returns (appending its reply),
        so holding the live list would misreport what went on the wire."""
        captured = []

        def _fake_chat(cfg, messages, **kwargs):
            captured.append([dict(m) for m in messages])
            return {"message": {"content": "a reply"}}

        with patch("src.claire.reply.engine.plan_query", return_value=[]), \
                patch("src.claire.reply.engine.extract_search_params_for_memory",
                      return_value={}), \
                patch("src.claire.reply.engine.run_tool_with_retries"), \
                patch("src.claire.reply.engine.extract_text_from_response",
                      return_value="a reply"), \
                patch("src.claire.reply.engine.chat_with_messages",
                      side_effect=_fake_chat):
            run_reply_engine(db=Mock(), cfg=self._cfg(), tts=None,
                             text="current question", dialogue_memory=memory)

        assert captured, "no messages captured"
        return captured[-1]

    @staticmethod
    def _history_of(sent):
        """Slice out the dialogue history: everything after the system prompt
        and before the current user utterance."""
        current = next(
            i for i, m in enumerate(sent)
            if m["role"] == "user" and m.get("content") == "current question"
        )
        return sent[1:current]

    def test_engine_sends_bounded_history(self):
        """The actual request body must be bounded: system + N history +
        current user. This is the end-to-end proof of the fix."""
        dm = DialogueMemory()
        _fill(dm, 300)
        sent = self._snapshot_send(dm)

        assert sent[0]["role"] == "system", "system prompt must be first"
        history = self._history_of(sent)
        assert len(history) <= MAX_MESSAGES, (
            f"engine sent {len(history)} history messages (cap {MAX_MESSAGES})"
        )
        assert _est_tokens(history) <= MAX_TOKENS, (
            f"engine sent {_est_tokens(history)} estimated history tokens "
            f"(cap {MAX_TOKENS})"
        )
        # Newest turn and the current utterance survive.
        assert history[-1]["role"] == "assistant"
        assert "assistant answer number 299" in history[-1]["content"]
        assert history[-2]["content"].startswith("user question number 299")
        _validator_ok([m for m in sent if m["role"] != "system"])

    def test_engine_bounds_legacy_memory_without_tool_getter(self):
        """Defence in depth: even a memory object that only offers the legacy
        ``get_recent_messages`` must not ship unbounded history."""
        dm = DialogueMemory()
        _fill(dm, 300)

        class Shim:
            """Mirrors DialogueMemory's legacy surface only."""

            def __init__(self, source):
                self._source = source

            def has_recent_messages(self):
                return True

            def get_recent_messages(self):
                return self._source.get_recent_messages()

        sent = self._snapshot_send(Shim(dm))
        history = self._history_of(sent)
        assert len(history) <= MAX_MESSAGES, (
            f"legacy path sent {len(history)} history messages"
        )


class TestBoundHistoryUnit:
    """Direct tests of the structure-aware truncator."""

    def test_oversized_newest_group_survives_in_full(self):
        msgs = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "x" * 40000},
            {"role": "assistant", "content": "y" * 40000},
        ]
        out = bound_conversation_history(msgs, max_tokens=100, max_messages=2)
        assert out == msgs[-2:], "the newest turn must survive even over budget"

    def test_complete_tool_group_is_kept_atomically(self):
        group = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c9", "type": "function",
                 "function": {"name": "webSearch", "arguments": {"q": "x"}}},
            ]},
            {"role": "tool", "tool_call_id": "c9", "content": "result"},
        ]
        out = bound_conversation_history(group, max_tokens=10000, max_messages=10)
        assert out == group
        _validator_ok(out)

    def test_unanswered_tool_call_group_is_dropped_whole(self):
        """An assistant tool_calls with no tool reply is not a safe head."""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "webSearch", "arguments": {}}},
            ]},
            {"role": "assistant", "content": "later text"},
        ]
        out = bound_conversation_history(msgs, max_tokens=10000, max_messages=10)
        assert all(m.get("role") != "tool" for m in out)
        _validator_ok(out)

    def test_dangling_leading_assistant_is_trimmed(self):
        msgs = [
            {"role": "assistant", "content": "orphan reply"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        out = bound_conversation_history(msgs, max_tokens=1000, max_messages=10)
        assert out[0]["role"] == "user"
        assert out == msgs[1:]

    def test_zero_budgets_disable_truncation(self):
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(50)]
        assert bound_conversation_history(msgs, max_tokens=0, max_messages=0) == msgs

    def test_estimate_grows_with_content(self):
        assert estimate_message_tokens({"role": "user", "content": "x" * 400}) > \
            estimate_message_tokens({"role": "user", "content": "x" * 40})


class TestConfigDefaults:
    def test_default_config_exposes_context_budget(self):
        from src.claire.config import get_default_config
        cfg = get_default_config()
        assert cfg["dialogue_memory_max_tokens"] == 8000
        assert cfg["dialogue_memory_max_messages"] == 40

    def test_settings_dataclass_has_budget_fields(self):
        import dataclasses
        from src.claire.config import Settings
        names = {f.name for f in dataclasses.fields(Settings)}
        assert "dialogue_memory_max_tokens" in names
        assert "dialogue_memory_max_messages" in names
