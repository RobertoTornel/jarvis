"""HTTP 400 must not be read as "this model cannot call tools" on its own.

Every 400 used to raise ``ToolsNotSupportedError`` whenever the request
carried a ``tools`` array. The reply engine catches that and permanently
downgrades the session to text-based tool calling: the system prompt is
rebuilt larger (measured 15001 -> 17822 chars, ~+700 tokens on every later
turn) and a retry is burned that 400s the same way. Observed on a news
query: 3.5 s + 4.1 s wasted on a gateway row with ``status=400,
tokens_in=0, "invalid request error"`` before a degraded answer.

These tests pin the narrower contract: only a 400 whose body actually
blames the tools parameter may downgrade.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def _response(status_code=400, text="", content=None, reason=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content if content is not None else text.encode()
    resp.reason = reason
    return resp


class TestIndicatesToolsUnsupported:
    def _check(self, **kwargs):
        from claire.llm.errors import indicates_tools_unsupported

        return indicates_tools_unsupported(_response(**kwargs))

    # ── bodies that DO blame tools ────────────────────────────────────

    @pytest.mark.parametrize(
        "text",
        [
            '{"error":"registry.ollama.ai/library/llama3 does not support tools"}',
            '{"error":{"message":"this model does not support tools","param":"tools"}}',
            '{"object":"error","message":"\\"tools\\" is not supported with this model"}',
            '{"error":{"message":"tool calling is not supported","type":"invalid_request_error"}}',
            '{"error":{"message":"Unsupported parameter: tool_choice"}}',
            '{"error":{"message":"unknown field tool_calls"}}',
        ],
    )
    def test_tool_blaming_bodies_are_detected(self, text):
        assert self._check(text=text) is True

    def test_structured_param_fields_are_detected(self):
        assert self._check(text='{"error":{"code":"tools_not_supported"}}') is True
        assert self._check(text='{"error":{"type":"tool_use_not_supported"}}') is True

    # ── bodies that do NOT blame tools ────────────────────────────────

    @pytest.mark.parametrize(
        "text",
        [
            '{"error":{"message":"invalid request error"}}',
            '{"error":{"message":"unknown model: foo/bar"}}',
            '{"error":{"message":"maximum context length exceeded: too many tokens"}}',
            '{"error":{"message":"rate limit exceeded"}}',
            '{"error":{"message":"invalid api key"}}',
            '{"error":{"message":"messages: field required"}}',
            "",
            "not json at all",
        ],
    )
    def test_non_tool_bodies_are_not_detected(self, text):
        assert self._check(text=text) is False

    def test_none_response_is_not_detected(self):
        from claire.llm.errors import indicates_tools_unsupported

        assert indicates_tools_unsupported(None) is False

    def test_unreadable_response_is_not_detected(self):
        from claire.llm.errors import indicates_tools_unsupported

        resp = MagicMock()
        type(resp).text = property(lambda self: (_ for _ in ()).throw(RuntimeError("gone")))
        type(resp).content = property(lambda self: (_ for _ in ()).throw(RuntimeError("gone")))
        assert indicates_tools_unsupported(resp) is False

    def test_bytes_body_is_decoded(self):
        raw = b'{"error":{"message":"does not support tools"}}'
        assert self._check(content=raw) is True

    def test_reason_phrase_alone_is_enough(self):
        assert self._check(text="", reason="unsupported tools parameter") is True
