"""Behaviour tests for the OpenAI-compatible LLM backend.

These tests pin the wire shape and response normalisation that lets the
reply engine consume responses from any OpenAI-compatible local server
(LM Studio, oMLX, llama.cpp's ``llama-server``, vLLM, LocalAI, ...) the
same way it consumes Ollama responses.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# The unit gate deselects any test that is not marked `unit`; a module-level
# pytestmark makes this whole file visible to it.
pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`


def _make_response(*, json_data=None, iter_lines=None, status_code=200, raise_http=None):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    if iter_lines is not None:
        resp.iter_lines.return_value = iter_lines
    if raise_http is not None:
        resp.raise_for_status.side_effect = raise_http
    else:
        resp.raise_for_status = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=None)
    return resp


# ---------------------------------------------------------------------------
# direct() — single-shot system+user
# ---------------------------------------------------------------------------


class TestOpenAICompatibleDirect:
    @patch("claire.llm.requests.post")
    def test_returns_assistant_text_from_choices(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "hello"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.direct("any-model", "sys", "user") == "hello"

    @patch("claire.llm.requests.post")
    def test_posts_to_chat_completions_endpoint(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1/")
        backend.direct("any-model", "sys", "user")

        url = mock_post.call_args[0][0]
        assert url == "http://localhost:1234/v1/chat/completions"

    @patch("claire.llm.requests.post")
    def test_sends_authorization_header_when_api_key_set(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1", api_key="sk-test")
        backend.direct("any-model", "sys", "user")

        headers = mock_post.call_args.kwargs.get("headers") or {}
        assert headers.get("Authorization") == "Bearer sk-test"

    @patch("claire.llm.requests.post")
    def test_omits_authorization_header_when_no_key(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")
        backend.direct("any-model", "sys", "user")

        headers = mock_post.call_args.kwargs.get("headers") or {}
        assert "Authorization" not in headers

    @patch("claire.llm.requests.post")
    def test_temperature_and_model_in_payload(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")
        backend.direct("my-model", "sys", "user", temperature=0.0)

        sent = mock_post.call_args.kwargs["json"]
        assert sent["model"] == "my-model"
        assert sent["temperature"] == 0.0
        assert sent["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ]

    @patch("claire.llm.requests.post")
    def test_returns_none_on_failure(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.side_effect = RuntimeError("server down")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.direct("any", "sys", "user") is None

    @patch("claire.llm.requests.post")
    def test_temperature_omitted_when_none(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        backend.direct("any-model", "sys", "user")  # default temperature=None

        sent = mock_post.call_args.kwargs["json"]
        assert "temperature" not in sent


# ---------------------------------------------------------------------------
# streaming() — SSE
# ---------------------------------------------------------------------------


class TestOpenAICompatibleStreaming:
    @patch("claire.llm.requests.post")
    def test_invokes_on_token_per_sse_chunk_and_returns_full_text(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        sse = [
            b'data: {"choices": [{"delta": {"content": "hel"}}]}',
            b"",
            b'data: {"choices": [{"delta": {"content": "lo"}}]}',
            b"",
            b"data: [DONE]",
        ]
        mock_post.return_value = _make_response(iter_lines=sse)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        seen: list[str] = []
        result = backend.streaming("any", "sys", "user", on_token=seen.append)

        assert seen == ["hel", "lo"]
        assert result == "hello"

    @patch("claire.llm.requests.post")
    def test_works_without_on_token_callback(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        sse = [
            b'data: {"choices": [{"delta": {"content": "x"}}]}',
            b"data: [DONE]",
        ]
        mock_post.return_value = _make_response(iter_lines=sse)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.streaming("any", "sys", "user") == "x"

    @patch("claire.llm.requests.post")
    def test_skips_keepalive_and_invalid_lines(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        sse = [
            b": keepalive",
            b"not-a-data-line",
            b'data: not-json',
            b'data: {"choices": [{"delta": {"content": "ok"}}]}',
        ]
        mock_post.return_value = _make_response(iter_lines=sse)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.streaming("any", "sys", "user") == "ok"

    @patch("claire.llm.requests.post")
    def test_returns_none_on_timeout(self, mock_post):
        import requests
        from claire.llm import OpenAICompatibleBackend

        mock_post.side_effect = requests.exceptions.Timeout("slow")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.streaming("any", "sys", "user") is None

    @patch("claire.llm.requests.post")
    def test_returns_none_on_connection_error(self, mock_post):
        import requests
        from claire.llm import OpenAICompatibleBackend

        mock_post.side_effect = requests.exceptions.ConnectionError("server down")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.streaming("any", "sys", "user") is None

    @patch("claire.llm.requests.post")
    def test_returns_none_on_http_error(self, mock_post):
        import requests
        from claire.llm import OpenAICompatibleBackend

        http_resp = MagicMock(status_code=500)
        err = requests.exceptions.HTTPError(response=http_resp)
        mock_post.return_value = _make_response(raise_http=err)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.streaming("any", "sys", "user") is None


# ---------------------------------------------------------------------------
# chat() — arbitrary messages, normalised to Ollama-shaped response
# ---------------------------------------------------------------------------


class TestOpenAICompatibleChat:
    @patch("claire.llm.requests.post")
    def test_lifts_choices_message_to_top_level(self, mock_post):
        """The reply engine reads ``resp['message']['content']``;
        OpenAI returns ``choices[0].message.content``. The backend must
        normalise the response so existing callers see Ollama's shape."""
        from claire.llm import OpenAICompatibleBackend

        upstream = {"choices": [{"message": {"content": "hello"}}]}
        mock_post.return_value = _make_response(json_data=upstream)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        result = backend.chat("any", [{"role": "user", "content": "hi"}])

        assert result is not None
        assert result["message"]["content"] == "hello"

    @patch("claire.llm.requests.post")
    def test_decodes_tool_call_arguments_string_to_dict(self, mock_post):
        """OpenAI returns ``tool_calls[*].function.arguments`` as a JSON
        string; Ollama returns it as a dict, and the reply engine
        expects a dict. The backend must decode."""
        from claire.llm import OpenAICompatibleBackend

        upstream = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "getWeather",
                                    "arguments": '{"location": "Tbilisi"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        mock_post.return_value = _make_response(json_data=upstream)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        result = backend.chat("any", [{"role": "user", "content": "weather"}])

        tc = result["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "getWeather"
        assert tc["function"]["arguments"] == {"location": "Tbilisi"}

    @patch("claire.llm.requests.post")
    def test_passes_tools_in_payload(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")
        backend.chat(
            "any",
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
        )

        sent = mock_post.call_args.kwargs["json"]
        assert sent["tools"][0]["function"]["name"] == "x"

    @patch("claire.llm.requests.post")
    def test_raises_tools_not_supported_when_body_blames_tools(self, mock_post):
        """Only a 400 whose body actually names tools may downgrade the
        session to text-based tool calling."""
        import requests
        from claire.llm import OpenAICompatibleBackend, ToolsNotSupportedError

        http_resp = MagicMock(status_code=400)
        http_resp.text = '{"error": {"message": "this model does not support tools"}}'
        err = requests.exceptions.HTTPError(response=http_resp)
        mock_post.return_value = _make_response(raise_http=err)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        with pytest.raises(ToolsNotSupportedError):
            backend.chat(
                "any",
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "x"}}],
            )

    @patch("claire.llm.requests.post")
    def test_400_not_about_tools_does_not_downgrade(self, mock_post):
        """Regression guard: every HTTP 400 used to be read as "tools not
        supported", which permanently downgraded the session to text tools,
        rebuilt a larger system prompt on every later turn and burned a
        retry. A 400 that never mentions tools must stay a plain failure."""
        import requests
        from claire.llm import OpenAICompatibleBackend, ToolsNotSupportedError

        http_resp = MagicMock(status_code=400)
        http_resp.text = '{"error": {"message": "invalid request error: unknown model"}}'
        err = requests.exceptions.HTTPError(response=http_resp)
        mock_post.return_value = _make_response(raise_http=err)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        try:
            result = backend.chat(
                "any",
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "x"}}],
            )
        except ToolsNotSupportedError:
            pytest.fail("a non-tools 400 must not downgrade the session")
        assert result is None

    @patch("claire.llm.requests.post")
    def test_400_with_empty_body_does_not_downgrade(self, mock_post):
        """No body means no evidence: stay with native tools."""
        import requests
        from claire.llm import OpenAICompatibleBackend, ToolsNotSupportedError

        http_resp = MagicMock(status_code=400)
        http_resp.text = ""
        http_resp.content = b""
        err = requests.exceptions.HTTPError(response=http_resp)
        mock_post.return_value = _make_response(raise_http=err)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        try:
            result = backend.chat(
                "any",
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "x"}}],
            )
        except ToolsNotSupportedError:
            pytest.fail("a bare 400 must not downgrade the session")
        assert result is None

    @patch("claire.llm.requests.post")
    def test_returns_none_on_400_without_tools(self, mock_post):
        import requests
        from claire.llm import OpenAICompatibleBackend

        http_resp = MagicMock(status_code=400)
        err = requests.exceptions.HTTPError(response=http_resp)
        mock_post.return_value = _make_response(raise_http=err)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.chat("any", [{"role": "user", "content": "hi"}]) is None

    @patch("claire.llm.requests.post")
    def test_propagates_connection_error(self, mock_post):
        """``chat`` re-raises ``ConnectionError`` so callers can distinguish
        an unreachable server from a transient HTTP failure."""
        import requests
        from claire.llm import OpenAICompatibleBackend

        mock_post.side_effect = requests.exceptions.ConnectionError("server down")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        with pytest.raises(requests.exceptions.ConnectionError):
            backend.chat("any", [{"role": "user", "content": "hi"}])

    @patch("claire.llm.requests.post")
    def test_returns_none_on_http_500_even_with_tools(self, mock_post):
        """Only HTTP 400 with tools means "model rejects native tools" —
        500 is a server-side failure that should degrade gracefully."""
        import requests
        from claire.llm import OpenAICompatibleBackend

        http_resp = MagicMock(status_code=500)
        err = requests.exceptions.HTTPError(response=http_resp)
        mock_post.return_value = _make_response(raise_http=err)
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert (
            backend.chat(
                "any",
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "x"}}],
            )
            is None
        )

    @patch("claire.llm.requests.post")
    def test_returns_none_on_timeout(self, mock_post):
        import requests
        from claire.llm import OpenAICompatibleBackend

        mock_post.side_effect = requests.exceptions.Timeout("slow")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.chat("any", [{"role": "user", "content": "hi"}]) is None

    @patch("claire.llm.requests.post")
    def test_returns_none_on_generic_exception(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.side_effect = RuntimeError("unexpected")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.chat("any", [{"role": "user", "content": "hi"}]) is None


# ---------------------------------------------------------------------------
# error messages must not leak configured URLs or API keys
# ---------------------------------------------------------------------------


class TestOpenAICompatibleErrorMessagesDoNotLeakUrls:
    """``requests.exceptions.HTTPError`` and ``ConnectionError`` ``str()``s
    typically embed the request URL — and the request URL can carry sensitive
    query strings (e.g. some hosted providers accept ``?api_key=…`` literally,
    and configured endpoints may include team/account identifiers). The chat
    backend prints these errors to stdout, which surfaces in the desktop log
    pane and any captured terminal session. The error message must include
    enough information for diagnosis (status code, exception class) without
    echoing the URL the user configured."""

    _SECRET_URL = "http://internal-server.example.com:1234/v1"

    @patch("claire.llm.requests.post")
    def test_http_error_message_does_not_leak_endpoint_url(self, mock_post, capsys):
        import requests
        from claire.llm import OpenAICompatibleBackend

        # Construct an HTTPError whose str() embeds the URL — exactly what
        # ``requests`` does in real failures.
        http_resp = MagicMock(status_code=401)
        http_resp.url = f"{self._SECRET_URL}/chat/completions"
        err = requests.exceptions.HTTPError(
            f"401 Client Error: Unauthorized for url: {self._SECRET_URL}/chat/completions",
            response=http_resp,
        )
        mock_post.return_value = _make_response(raise_http=err)
        backend = OpenAICompatibleBackend(self._SECRET_URL, api_key="sk-secret")

        backend.chat("any", [{"role": "user", "content": "hi"}])

        captured = capsys.readouterr()
        assert self._SECRET_URL not in captured.out, (
            "HTTPError message must not echo the configured endpoint URL"
        )
        # Status code must still be visible so users can diagnose 401 vs 5xx.
        assert "401" in captured.out

    @patch("claire.llm.requests.post")
    def test_connection_error_message_does_not_leak_endpoint_url(self, mock_post, capsys):
        import requests
        from claire.llm import OpenAICompatibleBackend

        # Real-world ConnectionError messages include the URL via the
        # underlying urllib3 exception.
        mock_post.side_effect = requests.exceptions.ConnectionError(
            f"HTTPConnectionPool(host='internal-server.example.com', port=1234): "
            f"Max retries exceeded with url: /v1/chat/completions"
        )
        backend = OpenAICompatibleBackend(self._SECRET_URL, api_key="sk-secret")

        # ``chat`` re-raises so the caller can detect "server unreachable";
        # the printed log line must not carry the URL or the API key.
        with pytest.raises(requests.exceptions.ConnectionError):
            backend.chat("any", [{"role": "user", "content": "hi"}])

        captured = capsys.readouterr()
        assert "internal-server.example.com" not in captured.out, (
            "ConnectionError message must not echo the configured host"
        )
        assert "sk-secret" not in captured.out

    @patch("claire.llm.requests.post")
    def test_generic_exception_message_does_not_leak_url_or_key(self, mock_post, capsys):
        from claire.llm import OpenAICompatibleBackend

        mock_post.side_effect = RuntimeError(
            f"unexpected: {self._SECRET_URL}?api_key=sk-secret"
        )
        backend = OpenAICompatibleBackend(self._SECRET_URL, api_key="sk-secret")

        backend.chat("any", [{"role": "user", "content": "hi"}])

        captured = capsys.readouterr()
        assert self._SECRET_URL not in captured.out
        assert "sk-secret" not in captured.out

    @patch("claire.llm.requests.post")
    def test_extra_options_merge_at_payload_root(self, mock_post):
        """OpenAI takes ``temperature`` / ``max_tokens`` at the payload
        root, not under an ``options`` nest like Ollama. The merge must
        land at the root."""
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        backend.chat(
            "any",
            [{"role": "user", "content": "hi"}],
            extra_options={"temperature": 0.5, "top_p": 0.9, "max_tokens": 256},
        )

        sent = mock_post.call_args.kwargs["json"]
        assert sent["temperature"] == 0.5
        assert sent["top_p"] == 0.9
        assert sent["max_tokens"] == 256
        # Must NOT live under a nested 'options' key (that's the Ollama shape).
        assert "options" not in sent


# ---------------------------------------------------------------------------
# embed() — POST /embeddings
# ---------------------------------------------------------------------------


class TestOpenAICompatibleEmbed:
    @patch("claire.llm.requests.post")
    def test_returns_vector_from_data_array(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        resp = MagicMock()
        resp.json.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
        resp.raise_for_status = MagicMock()
        mock_post.return_value = resp
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.embed("hello", "text-embedding-3-small") == [0.1, 0.2, 0.3]

    @patch("claire.llm.requests.post")
    def test_returns_none_on_error(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.side_effect = RuntimeError("boom")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.embed("hello", "text-embedding-3-small") is None

    @patch("claire.llm.requests.post")
    def test_sends_authorization_when_api_key_set(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        resp = MagicMock()
        resp.json.return_value = {"data": [{"embedding": [0.1]}]}
        resp.raise_for_status = MagicMock()
        mock_post.return_value = resp
        backend = OpenAICompatibleBackend("http://localhost:1234/v1", api_key="sk-x")
        backend.embed("hello", "model")

        headers = mock_post.call_args.kwargs.get("headers") or {}
        assert headers.get("Authorization") == "Bearer sk-x"


# ---------------------------------------------------------------------------
# list_models() — GET /models
# ---------------------------------------------------------------------------


class TestOpenAICompatibleListModels:
    @patch("claire.llm.requests.get")
    def test_returns_model_ids(self, mock_get):
        from claire.llm import OpenAICompatibleBackend

        resp = MagicMock()
        resp.json.return_value = {
            "data": [
                {"id": "gpt-4o-mini"},
                {"id": "lmstudio-community/gemma-3-4b-it-GGUF"},
            ]
        }
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        names = backend.list_models()

        assert names == ["gpt-4o-mini", "lmstudio-community/gemma-3-4b-it-GGUF"]

    @patch("claire.llm.requests.get")
    def test_returns_empty_list_on_failure(self, mock_get):
        from claire.llm import OpenAICompatibleBackend

        mock_get.side_effect = RuntimeError("boom")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.list_models() == []

    @patch("claire.llm.requests.get")
    def test_returns_empty_list_when_data_field_missing(self, mock_get):
        from claire.llm import OpenAICompatibleBackend

        resp = MagicMock()
        resp.json.return_value = {}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.list_models() == []


# ---------------------------------------------------------------------------
# warm_up() — the inference request IS the probe
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenAICompatibleWarmUp:
    """``warm_up`` sends a minimal chat completion (``max_tokens=1``) to
    force the runtime to load the model into memory, and reports that
    request's outcome.

    Regression guard: an earlier revision gated the inference behind a
    ``GET /models`` reachability check capped at ``min(timeout*0.25, 5)`` s.
    Against a gateway that proxies a large catalogue, listing models costs
    more than the cap (measured: 9508 entries, 4.5 MB, 5.0-5.5 s just to
    response headers), so the gate expired on every call, ``warm_up``
    returned False, and the inference that actually warms the model never
    ran. These tests pin that the catalogue is not consulted at all and
    that the inference alone decides the result.
    """

    @staticmethod
    def _backend():
        from claire.llm import OpenAICompatibleBackend

        return OpenAICompatibleBackend("http://localhost:1234/v1")

    @staticmethod
    def _ok_post(mock_post):
        post_resp = MagicMock()
        post_resp.__enter__.return_value.ok = True
        mock_post.return_value = post_resp
        return post_resp

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_never_lists_models(self, mock_post, mock_get):
        """The catalogue endpoint must not be touched: consulting it is
        exactly what used to turn every warmup into a false negative."""
        self._ok_post(mock_post)

        assert self._backend().warm_up("gpt-4o-mini") is True

        assert not mock_get.called
        mock_post.assert_called_once()

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_succeeds_on_inference_ok(self, mock_post, mock_get):
        self._ok_post(mock_post)

        assert self._backend().warm_up("gpt-4o-mini") is True

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_inference_payload_is_minimal(self, mock_post, mock_get):
        """The inference request uses max_tokens=1 and a short message."""
        self._ok_post(mock_post)

        self._backend().warm_up("gpt-4o-mini")

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["model"] == "gpt-4o-mini"
        assert payload["messages"] == [{"role": "user", "content": "ping"}]
        assert payload["max_tokens"] == 1
        assert payload["stream"] is False

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_accepts_keep_alive_kwarg_and_ignores_it(self, mock_post, mock_get):
        """The shared ``warm_up_chat_model`` helper always passes
        ``keep_alive`` (Ollama parity). The OpenAI-compatible backend must
        accept the kwarg; before that fix it raised ``TypeError`` and every
        warmup silently failed."""
        self._ok_post(mock_post)

        assert self._backend().warm_up("gpt-4o-mini", keep_alive="1m") is True

        _, kwargs = mock_post.call_args
        assert "keep_alive" not in kwargs["json"]

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_gives_the_whole_budget_to_the_inference(self, mock_post, mock_get):
        """No slice of the budget is reserved for a catalogue listing."""
        self._ok_post(mock_post)

        self._backend().warm_up("gpt-4o-mini", timeout_sec=60.0)
        assert mock_post.call_args[1]["timeout"] == 60.0

        self._backend().warm_up("gpt-4o-mini", timeout_sec=3.0)
        assert mock_post.call_args[1]["timeout"] == 3.0

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_returns_false_when_inference_not_ok(self, mock_post, mock_get):
        post_resp = MagicMock()
        post_resp.__enter__.return_value.ok = False
        mock_post.return_value = post_resp

        assert self._backend().warm_up("gpt-4o-mini") is False

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_returns_false_when_server_unreachable(self, mock_post, mock_get):
        """A refused connection fails immediately on the inference request,
        so no separate reachability probe is needed to detect it."""
        import requests as _requests

        mock_post.side_effect = _requests.exceptions.ConnectionError("refused")

        assert self._backend().warm_up("gpt-4o-mini") is False

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_returns_false_on_unexpected_error(self, mock_post, mock_get):
        mock_post.side_effect = RuntimeError("model load failed")

        assert self._backend().warm_up("gpt-4o-mini") is False

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_sends_auth_header_when_key_set(self, mock_post, mock_get):
        from claire.llm import OpenAICompatibleBackend

        self._ok_post(mock_post)

        OpenAICompatibleBackend("http://localhost:1234/v1", api_key="sk-test").warm_up(
            "gpt-4o-mini"
        )

        assert mock_post.call_args[1]["headers"]["Authorization"] == "Bearer sk-test"

    @patch("claire.llm.requests.get")
    @patch("claire.llm.requests.post")
    def test_warmup_omits_auth_header_when_no_key(self, mock_post, mock_get):
        self._ok_post(mock_post)

        self._backend().warm_up("gpt-4o-mini")

        assert "Authorization" not in mock_post.call_args[1]["headers"]

    # ── guard cases ───────────────────────────────────────────────────

    @patch("claire.llm.requests.post")
    def test_warmup_fails_for_empty_base_url(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        assert OpenAICompatibleBackend("").warm_up("gpt-4o-mini") is False
        assert not mock_post.called

    @patch("claire.llm.requests.post")
    def test_warmup_fails_for_empty_model(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.warm_up("") is False
        assert backend.warm_up(None) is False
        assert not mock_post.called



# ---------------------------------------------------------------------------
# _normalise_response — passthrough and edge cases
# ---------------------------------------------------------------------------


class TestNormaliseResponse:
    """``_normalise_response`` is the wire-shape translator that lets the
    reply engine consume OpenAI responses through the same parsing path
    it uses for Ollama. These tests pin the contract for the shapes a
    real server might emit."""

    def test_ollama_shaped_response_passes_through(self):
        """Hybrid servers that already return ``message`` at the top
        level (some llama.cpp builds do this) must not be re-wrapped."""
        from claire.llm.openai_compatible import _normalise_response

        upstream = {"message": {"content": "hi"}}

        assert _normalise_response(upstream) == upstream

    def test_arguments_already_dict_is_preserved(self):
        """Some servers pre-decode tool-call arguments to a dict. The
        normaliser must leave them alone rather than passing them
        through ``json.loads`` (which would raise)."""
        from claire.llm.openai_compatible import _normalise_response

        upstream = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "x",
                                    "arguments": {"already": "dict"},
                                }
                            }
                        ],
                    }
                }
            ]
        }

        result = _normalise_response(upstream)

        tc = result["message"]["tool_calls"][0]
        assert tc["function"]["arguments"] == {"already": "dict"}

    def test_malformed_arguments_string_left_as_is(self):
        """If a server returns invalid JSON in ``arguments``, the
        normaliser leaves the string in place so the engine's
        content-mode parser can still attempt recovery — better than
        raising and losing the whole turn."""
        from claire.llm.openai_compatible import _normalise_response

        upstream = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "x",
                                    "arguments": "not valid json",
                                }
                            }
                        ],
                    }
                }
            ]
        }

        result = _normalise_response(upstream)

        tc = result["message"]["tool_calls"][0]
        assert tc["function"]["arguments"] == "not valid json"


# ---------------------------------------------------------------------------
# chat() — message sanitization
# ---------------------------------------------------------------------------


class TestOpenAICompatibleSanitizesMessages:
    """OpenAICompatibleBackend.chat() strips non-standard fields from
    messages before sending them to the wire."""

    @patch("claire.llm.openai_compatible.requests.post")
    def test_tool_name_not_in_wire_payload(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "done"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        backend.chat(
            "gemma-4-e2b",
            [
                {"role": "user", "content": "How's the weather?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "1", "type": "function", "function": {"name": "getWeather"}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "1",
                    "content": "Error: no location",
                    "tool_name": "getWeather",
                    "tool_failed": True,
                },
            ],
        )

        sent = mock_post.call_args.kwargs["json"]
        tool_msg = sent["messages"][2]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "1"
        assert tool_msg["content"] == "Error: no location"
        assert "tool_name" not in tool_msg, "tool_name field must be stripped"
        assert "tool_failed" not in tool_msg, "tool_failed field must be stripped"

    @patch("claire.llm.openai_compatible.requests.post")
    def test_system_is_context_injected_stripped(self, mock_post):
        from claire.llm import OpenAICompatibleBackend

        mock_post.return_value = _make_response(
            json_data={"choices": [{"message": {"content": "hello"}}]}
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        backend.chat(
            "gemma-4-e2b",
            [
                {
                    "role": "system",
                    "content": "You are a helpful assistant.",
                    "_is_context_injected": True,
                },
                {"role": "user", "content": "Hi"},
            ],
        )

        sent = mock_post.call_args.kwargs["json"]
        sys_msg = sent["messages"][0]
        assert sys_msg["role"] == "system"
        assert sys_msg["content"] == "You are a helpful assistant."
        assert "_is_context_injected" not in sys_msg
