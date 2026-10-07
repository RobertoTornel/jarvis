"""
Tests for the LLM thinking mode feature.

Verifies that the ``llm_thinking_enabled`` config option correctly controls
the ``think`` parameter sent to Ollama across all call sites.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from claire.config import get_default_config


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestThinkingConfig:
    """Config layer tests for thinking settings."""

    def test_default_config_has_chat_thinking_disabled(self):
        """llm_thinking_enabled should default to False."""
        config = get_default_config()
        assert "llm_thinking_enabled" in config
        assert config["llm_thinking_enabled"] is False

    def test_default_config_has_intent_judge_thinking_disabled(self):
        """intent_judge_thinking_enabled should default to False."""
        config = get_default_config()
        assert "intent_judge_thinking_enabled" in config
        assert config["intent_judge_thinking_enabled"] is False


# ---------------------------------------------------------------------------
# llm.py — payload construction
# ---------------------------------------------------------------------------

class TestLlmThinkingPayload:
    """Verify the ``think`` key appears in Ollama request payloads."""

    def _capture_payload(self, mock_post, *, expect_stream=False):
        """Extract the JSON payload from the first call to requests.post."""
        assert mock_post.called, "requests.post was never called"
        _, kwargs = mock_post.call_args
        payload = kwargs.get("json") or {}
        return payload

    @patch("claire.llm.requests.post")
    def test_call_llm_direct_thinking_false(self, mock_post):
        from claire.llm import call_llm_direct

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": {"content": "ok"}}
        mock_post.return_value = mock_resp

        call_llm_direct("http://localhost:11434", "gemma4:e2b", "sys", "hi", thinking=False)
        payload = self._capture_payload(mock_post)
        assert payload["think"] is False

    @patch("claire.llm.requests.post")
    def test_call_llm_direct_thinking_true(self, mock_post):
        from claire.llm import call_llm_direct

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": {"content": "ok"}}
        mock_post.return_value = mock_resp

        call_llm_direct("http://localhost:11434", "gemma4:e2b", "sys", "hi", thinking=True)
        payload = self._capture_payload(mock_post)
        assert payload["think"] is True

    @patch("claire.llm.requests.post")
    def test_call_llm_direct_thinking_defaults_false(self, mock_post):
        from claire.llm import call_llm_direct

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": {"content": "ok"}}
        mock_post.return_value = mock_resp

        call_llm_direct("http://localhost:11434", "gemma4:e2b", "sys", "hi")
        payload = self._capture_payload(mock_post)
        assert payload["think"] is False

    @patch("claire.llm.requests.post")
    def test_call_llm_streaming_thinking(self, mock_post):
        from claire.llm import call_llm_streaming

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = [
            json.dumps({"message": {"content": "hi"}}).encode()
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        call_llm_streaming("http://localhost:11434", "gemma4:e2b", "sys", "hi", thinking=True)
        payload = self._capture_payload(mock_post)
        assert payload["think"] is True

    @patch("claire.llm.requests.post")
    def test_chat_with_messages_thinking(self, mock_post):
        from claire.llm import chat_with_messages

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": {"content": "ok"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        msgs = [{"role": "user", "content": "hi"}]
        chat_with_messages("http://localhost:11434", "gemma4:e2b", msgs, thinking=True)
        payload = self._capture_payload(mock_post)
        assert payload["think"] is True

    @patch("claire.llm.requests.post")
    def test_chat_with_messages_thinking_defaults_false(self, mock_post):
        from claire.llm import chat_with_messages

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": {"content": "ok"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        msgs = [{"role": "user", "content": "hi"}]
        chat_with_messages("http://localhost:11434", "gemma4:e2b", msgs)
        payload = self._capture_payload(mock_post)
        assert payload["think"] is False


# ---------------------------------------------------------------------------
# Intent judge
# ---------------------------------------------------------------------------

class TestIntentJudgeThinking:
    """Intent judge respects the thinking config."""

    def test_config_default_thinking_false(self):
        from claire.listening.intent_judge import IntentJudgeConfig
        config = IntentJudgeConfig()
        assert config.thinking is False

    def test_config_accepts_thinking_true(self):
        from claire.listening.intent_judge import IntentJudgeConfig
        config = IntentJudgeConfig(thinking=True)
        assert config.thinking is True

    def test_create_intent_judge_passes_thinking(self):
        """create_intent_judge should read intent_judge_thinking_enabled from cfg."""
        from claire.listening.intent_judge import create_intent_judge

        cfg = MagicMock()
        cfg.wake_word = "claire"
        cfg.wake_aliases = []
        cfg.fast_model = "gemma4:e2b"
        cfg.ollama_base_url = "http://localhost:11434"
        cfg.intent_judge_timeout_sec = 10.0
        cfg.intent_judge_thinking_enabled = True

        judge = create_intent_judge(cfg)
        assert judge is not None
        assert judge.config.thinking is True

    def test_create_intent_judge_independent_from_chat_thinking(self):
        """Intent judge thinking should be independent from chat thinking."""
        from claire.listening.intent_judge import create_intent_judge

        cfg = MagicMock()
        cfg.wake_word = "claire"
        cfg.wake_aliases = []
        cfg.fast_model = "gemma4:e2b"
        cfg.ollama_base_url = "http://localhost:11434"
        cfg.intent_judge_timeout_sec = 10.0
        cfg.llm_thinking_enabled = True
        cfg.intent_judge_thinking_enabled = False

        judge = create_intent_judge(cfg)
        assert judge.config.thinking is False


# ---------------------------------------------------------------------------
# Settings window metadata
# ---------------------------------------------------------------------------

class TestSettingsWindowThinking:
    """Settings window includes both thinking fields."""

    def test_field_metadata_includes_chat_thinking(self):
        from desktop_app.settings_window import FIELD_METADATA
        keys = [fm.key for fm in FIELD_METADATA]
        assert "llm_thinking_enabled" in keys

    def test_chat_thinking_field_is_bool_in_llm_category(self):
        from desktop_app.settings_window import FIELD_METADATA
        field = next(fm for fm in FIELD_METADATA if fm.key == "llm_thinking_enabled")
        assert field.field_type == "bool"
        assert field.category == "llm"

    def test_field_metadata_includes_intent_judge_thinking(self):
        from desktop_app.settings_window import FIELD_METADATA
        keys = [fm.key for fm in FIELD_METADATA]
        assert "intent_judge_thinking_enabled" in keys

    def test_intent_judge_thinking_field_is_bool_in_llm_category(self):
        from desktop_app.settings_window import FIELD_METADATA
        field = next(fm for fm in FIELD_METADATA if fm.key == "intent_judge_thinking_enabled")
        assert field.field_type == "bool"
        assert field.category == "llm"


# ---------------------------------------------------------------------------
# Timeout / error paths — regression test for missing debug_log import
# ---------------------------------------------------------------------------

class TestCallLlmDirectFailurePaths:
    """Exercises the exception branches in call_llm_direct.

    These branches call debug_log; a missing import would surface here as a
    NameError instead of the intended graceful None return.
    """

    def test_timeout_returns_none(self):
        import requests
        from claire.llm import call_llm_direct

        with patch('claire.llm.requests.post', side_effect=requests.exceptions.Timeout):
            result = call_llm_direct(
                base_url="http://localhost:99999",
                chat_model="test-model",
                system_prompt="sys",
                user_content="hi",
                timeout_sec=0.1,
            )
        assert result is None

    def test_request_exception_returns_none(self):
        from claire.llm import call_llm_direct

        with patch('claire.llm.requests.post', side_effect=ConnectionError("boom")):
            result = call_llm_direct(
                base_url="http://localhost:99999",
                chat_model="test-model",
                system_prompt="sys",
                user_content="hi",
                timeout_sec=0.1,
            )
        assert result is None
