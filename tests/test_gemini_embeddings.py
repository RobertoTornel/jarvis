"""Gemini embeddings: request shape, dimension pinning and failure honesty.

The embedding path had no reachable service before this work — the configured
route followed ``llm_provider`` to a local gateway that rejects embedding
models, and memory search silently degraded to keyword matching. These tests
cover the replacement end of that pipe with a stubbed HTTP layer (no network):
the native Gemini ``embedContent`` / ``batchEmbedContents`` calls, the pinned
768-float output the local index requires, the asymmetric task type on each
side of retrieval, and the guarantee that a failure falls back to keywords
without writing anything the index cannot read back.

Nothing here makes a live call; a real key is only exercised by hand (see the
module docstring in ``claire/llm/gemini.py``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`

# A fixture value, not a credential. Asserting the header carries it is how the
# tests prove the key travels in ``x-goog-api-key`` and never in the URL.
_STUB_KEY = "stub-gemini-key-for-tests"

_DIM = 768


def _vector(value: float = 0.1, length: int = _DIM) -> list:
    return [value] * length


class _Response:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Recorder:
    """Stub for ``requests.post``: records every call, replays canned replies."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, json=None, headers=None, timeout=None):  # noqa: A002 - requests' kwarg
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if not self.responses:
            raise AssertionError(f"unexpected extra HTTP call to {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _backend(monkeypatch, *responses, api_key: str = _STUB_KEY, base_url: str = ""):
    from claire.llm.gemini import GEMINI_BASE_URL, GeminiEmbeddingBackend

    recorder = _Recorder(*responses)
    monkeypatch.setattr("claire.llm.gemini.requests.post", recorder)
    backend = GeminiEmbeddingBackend(base_url or GEMINI_BASE_URL, api_key=api_key)
    return backend, recorder


# ── request shape ─────────────────────────────────────────────────────────


class TestEmbedContentRequest:
    def test_model_id_and_endpoint_are_the_native_resource(self, monkeypatch):
        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        result = backend.embed("the user prefers Celsius", "gemini-embedding-2")

        assert result is not None and len(result) == _DIM
        call = http.calls[0]
        assert call["url"] == (
            "https://generativelanguage.googleapis.com/v1beta"
            "/models/gemini-embedding-2:embedContent"
        )

    def test_auth_travels_in_the_api_key_header_never_in_the_url(self, monkeypatch):
        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        backend.embed("hello", "gemini-embedding-2")

        call = http.calls[0]
        assert call["headers"]["x-goog-api-key"] == _STUB_KEY
        assert "key=" not in call["url"]
        assert _STUB_KEY not in call["url"]

    def test_output_dimensionality_is_pinned_to_the_index_width(self, monkeypatch):
        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        backend.embed("hello", "gemini-embedding-2")

        body = http.calls[0]["json"]
        assert body["outputDimensionality"] == _DIM
        assert body["model"] == "models/gemini-embedding-2"

    def test_embedding_2_document_uses_the_documented_title_prefix(self, monkeypatch):
        from claire.llm import TASK_RETRIEVAL_DOCUMENT

        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        backend.embed("the user prefers Celsius", "gemini-embedding-2",
                      task_type=TASK_RETRIEVAL_DOCUMENT)

        body = http.calls[0]["json"]
        assert body["content"]["parts"][0]["text"] == "title: none | text: the user prefers Celsius"
        # Embedding 2 replaced taskType with the prefix; sending both is not
        # the documented shape.
        assert "taskType" not in body

    def test_embedding_2_query_uses_the_search_result_prefix(self, monkeypatch):
        from claire.llm import TASK_RETRIEVAL_QUERY

        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        backend.embed("what temperature does the user like",
                      "gemini-embedding-2", task_type=TASK_RETRIEVAL_QUERY)

        body = http.calls[0]["json"]
        assert body["content"]["parts"][0]["text"] == (
            "task: search result | query: what temperature does the user like"
        )

    def test_embedding_001_document_uses_the_native_task_type(self, monkeypatch):
        from claire.llm import TASK_RETRIEVAL_DOCUMENT

        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        backend.embed("the user prefers Celsius", "gemini-embedding-001",
                      task_type=TASK_RETRIEVAL_DOCUMENT)

        body = http.calls[0]["json"]
        assert body["taskType"] == TASK_RETRIEVAL_DOCUMENT
        # Embedding 1 takes the task type as a field, not in the text.
        assert body["content"]["parts"][0]["text"] == "the user prefers Celsius"

    def test_embedding_001_query_uses_the_other_task_type(self, monkeypatch):
        from claire.llm import TASK_RETRIEVAL_QUERY

        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        backend.embed("what does the user like", "gemini-embedding-001",
                      task_type=TASK_RETRIEVAL_QUERY)

        assert http.calls[0]["json"]["taskType"] == TASK_RETRIEVAL_QUERY

    def test_no_task_type_means_a_symmetric_request(self, monkeypatch):
        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        backend.embed("hello", "gemini-embedding-001")

        body = http.calls[0]["json"]
        assert "taskType" not in body
        assert body["content"]["parts"][0]["text"] == "hello"

    def test_unknown_task_type_is_dropped_rather_than_sent(self, monkeypatch):
        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        backend.embed("hello", "gemini-embedding-001", task_type="NOT_A_TASK")

        assert "taskType" not in http.calls[0]["json"]

    def test_custom_base_url_is_honoured(self, monkeypatch):
        backend, http = _backend(
            monkeypatch,
            _Response({"embedding": {"values": _vector()}}),
            base_url="http://127.0.0.1:9999/v1beta",
        )

        backend.embed("hello", "gemini-embedding-2")

        assert http.calls[0]["url"].startswith("http://127.0.0.1:9999/v1beta/models/")

    def test_timeout_is_forwarded(self, monkeypatch):
        backend, http = _backend(
            monkeypatch, _Response({"embedding": {"values": _vector()}})
        )

        backend.embed("hello", "gemini-embedding-2", timeout_sec=3.5)

        assert http.calls[0]["timeout"] == 3.5


# ── dimension safety ──────────────────────────────────────────────────────


class TestDimensionSafety:
    def test_wrong_length_vector_is_refused_not_returned(self, monkeypatch):
        """An embedding whose length does not match the index must never be
        handed to the caller — that is the whole point of pinning 768."""
        backend, _ = _backend(
            monkeypatch, _Response({"embedding": {"values": [0.1, 0.2, 0.3]}})
        )

        assert backend.embed("hello", "gemini-embedding-2") is None

    def test_missing_values_is_refused(self, monkeypatch):
        backend, _ = _backend(monkeypatch, _Response({"embedding": {}}))

        assert backend.embed("hello", "gemini-embedding-2") is None

    def test_non_numeric_values_are_refused(self, monkeypatch):
        backend, _ = _backend(
            monkeypatch, _Response({"embedding": {"values": ["nope"] * _DIM}})
        )

        assert backend.embed("hello", "gemini-embedding-2") is None

    def test_the_pinned_dimension_is_the_store_dimension(self):
        from claire.llm.gemini import GEMINI_OUTPUT_DIMENSION
        from claire.utils.embedding_vector import EMBEDDING_DIMENSION

        assert GEMINI_OUTPUT_DIMENSION == EMBEDDING_DIMENSION == 768

    def test_the_database_builds_its_index_at_that_dimension(self, tmp_path, monkeypatch):
        """The constant is only useful if the store really is built with it."""
        import claire.utils.vector_store as vs
        from claire.memory.db import Database
        from claire.utils.embedding_vector import EMBEDDING_DIMENSION

        seen = {}

        def _spy(path, dimension):
            seen["dimension"] = dimension
            return vs.PythonVectorStore(path)

        monkeypatch.setattr(vs, "get_best_vector_store", _spy)
        db = Database(str(tmp_path / "diary.db"))
        try:
            assert seen["dimension"] == EMBEDDING_DIMENSION
        finally:
            db.close()


# ── batching ──────────────────────────────────────────────────────────────


class TestBatchEmbedding:
    def test_batch_uses_batch_endpoint_and_keeps_input_order(self, monkeypatch):
        texts = ["first memory", "second memory", "third memory"]
        payload = {"embeddings": [
            {"values": _vector(0.1)},
            {"values": _vector(0.2)},
            {"values": _vector(0.3)},
        ]}
        backend, http = _backend(monkeypatch, _Response(payload))

        result = backend.embed_batch(texts, "gemini-embedding-2")

        assert [v[0] for v in result] == [pytest.approx(0.1), pytest.approx(0.2), pytest.approx(0.3)]
        call = http.calls[0]
        assert call["url"].endswith("/models/gemini-embedding-2:batchEmbedContents")
        requests_body = call["json"]["requests"]
        assert len(requests_body) == 3
        assert [r["content"]["parts"][0]["text"] for r in requests_body] == texts
        assert all(r["outputDimensionality"] == _DIM for r in requests_body)
        assert all(r["model"] == "models/gemini-embedding-2" for r in requests_body)

    def test_batch_applies_the_document_task_type(self, monkeypatch):
        from claire.llm import TASK_RETRIEVAL_DOCUMENT

        backend, http = _backend(
            monkeypatch, _Response({"embeddings": [{"values": _vector()}]})
        )

        backend.embed_batch(["a memory"], "gemini-embedding-001",
                            task_type=TASK_RETRIEVAL_DOCUMENT)

        assert http.calls[0]["json"]["requests"][0]["taskType"] == TASK_RETRIEVAL_DOCUMENT

    def test_long_runs_are_chunked_not_sent_as_one_request(self, monkeypatch):
        texts = [f"memory {i}" for i in range(205)]
        backend, http = _backend(
            monkeypatch,
            _Response({"embeddings": [{"values": _vector()} for _ in range(100)]}),
            _Response({"embeddings": [{"values": _vector()} for _ in range(100)]}),
            _Response({"embeddings": [{"values": _vector()} for _ in range(5)]}),
        )

        result = backend.embed_batch(texts, "gemini-embedding-2")

        assert len(http.calls) == 3
        assert [len(c["json"]["requests"]) for c in http.calls] == [100, 100, 5]
        assert len(result) == 205
        assert all(v is not None for v in result)

    def test_short_batch_response_degrades_to_none_positionally(self, monkeypatch):
        """A response that does not line up with the inputs is discarded
        rather than shifted onto the wrong memories."""
        backend, _ = _backend(
            monkeypatch, _Response({"embeddings": [{"values": _vector()}]})
        )

        result = backend.embed_batch(["a", "b", "c"], "gemini-embedding-2")

        assert result == [None, None, None]


# ── failure behaviour ─────────────────────────────────────────────────────


class TestFailureBehaviour:
    def test_http_error_returns_none_instead_of_raising(self, monkeypatch):
        backend, _ = _backend(monkeypatch, _Response({}, status_code=403))

        assert backend.embed("hello", "gemini-embedding-2") is None

    def test_network_error_returns_none_instead_of_raising(self, monkeypatch):
        import requests

        backend, _ = _backend(monkeypatch, requests.ConnectionError("unreachable"))

        assert backend.embed("hello", "gemini-embedding-2") is None

    def test_batch_network_error_returns_all_none(self, monkeypatch):
        import requests

        backend, _ = _backend(monkeypatch, requests.ConnectionError("unreachable"))

        assert backend.embed_batch(["a", "b"], "gemini-embedding-2") == [None, None]

    def test_missing_key_makes_no_request_at_all(self, monkeypatch):
        backend, http = _backend(monkeypatch, api_key="")

        assert backend.embed("hello", "gemini-embedding-2") is None
        assert backend.embed_batch(["hello"], "gemini-embedding-2") == [None]
        assert http.calls == []

    def test_errors_do_not_leak_the_key_into_the_log(self, monkeypatch, capsys):
        import requests

        backend, _ = _backend(monkeypatch, requests.ConnectionError(
            f"boom https://example.invalid/?key={_STUB_KEY}"
        ))

        backend.embed("hello", "gemini-embedding-2")

        captured = capsys.readouterr()
        assert _STUB_KEY not in captured.out
        assert _STUB_KEY not in captured.err


# ── factory wiring ────────────────────────────────────────────────────────


def _cfg(**overrides):
    base = dict(
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:20129/v1",
        llm_api_key="router-key-not-a-gemini-key",
        llm_chat_model="cmd/deepseek/deepseek-v4.1-flash",
        embedding_provider="gemini",
        embedding_base_url="",
        embedding_api_key=_STUB_KEY,
        embedding_model="gemini-embedding-2",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_embed_model="nomic-embed-text",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestFactoryDispatch:
    def test_gemini_provider_builds_the_gemini_backend(self):
        from claire.llm import GeminiEmbeddingBackend, get_embedding_backend
        from claire.llm.gemini import GEMINI_BASE_URL

        backend = get_embedding_backend(_cfg())

        assert isinstance(backend, GeminiEmbeddingBackend)
        assert backend.base_url == GEMINI_BASE_URL

    def test_base_url_override_wins(self):
        from claire.llm import get_embedding_backend

        backend = get_embedding_backend(_cfg(embedding_base_url="https://proxy.internal/v1beta"))

        assert backend.base_url == "https://proxy.internal/v1beta"

    def test_google_spelling_resolves_to_gemini(self):
        from claire.llm import GeminiEmbeddingBackend, get_embedding_backend

        assert isinstance(
            get_embedding_backend(_cfg(embedding_provider="google")),
            GeminiEmbeddingBackend,
        )

    def test_llm_api_key_is_never_sent_to_google(self, monkeypatch):
        """The chat router key belongs to the router; reusing it for Google
        would be both useless and a credential leak."""
        from claire.llm import get_embedding_backend

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        backend = get_embedding_backend(
            _cfg(embedding_api_key="", llm_api_key="router-key")
        )

        assert backend._api_key is None

    def test_gemini_api_key_env_is_used_when_config_is_empty(self, monkeypatch):
        from claire.llm import get_embedding_backend

        monkeypatch.setenv("GEMINI_API_KEY", _STUB_KEY)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        backend = get_embedding_backend(_cfg(embedding_api_key=""))

        assert backend._api_key == _STUB_KEY

    def test_google_api_key_env_is_the_second_choice(self, monkeypatch):
        from claire.llm import get_embedding_backend

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", _STUB_KEY)

        backend = get_embedding_backend(_cfg(embedding_api_key=""))

        assert backend._api_key == _STUB_KEY

    def test_config_key_outranks_the_environment(self, monkeypatch):
        from claire.llm import get_embedding_backend

        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        backend = get_embedding_backend(_cfg(embedding_api_key=_STUB_KEY))

        assert backend._api_key == _STUB_KEY

    def test_gemini_is_embeddings_only_not_a_chat_provider(self):
        """``llm_provider: "gemini"`` must keep behaving like any other
        unknown chat provider — Ollama — because this backend cannot chat."""
        from claire.llm import OllamaBackend, get_llm_backend

        assert isinstance(get_llm_backend(_cfg(llm_provider="gemini")), OllamaBackend)

    def test_existing_providers_still_resolve(self):
        from claire.llm import (
            OllamaBackend,
            OpenAICompatibleBackend,
            get_embedding_backend,
        )

        assert isinstance(
            get_embedding_backend(_cfg(embedding_provider="ollama")), OllamaBackend
        )
        assert isinstance(
            get_embedding_backend(_cfg(embedding_provider="openai_compatible")),
            OpenAICompatibleBackend,
        )
        # Empty provider still inherits the chat provider.
        assert isinstance(
            get_embedding_backend(_cfg(embedding_provider="")),
            OpenAICompatibleBackend,
        )

    def test_unknown_provider_still_falls_back_to_ollama(self):
        from claire.llm import OllamaBackend, get_embedding_backend

        assert isinstance(
            get_embedding_backend(_cfg(embedding_provider="anthropic")), OllamaBackend
        )


# ── config resolution ─────────────────────────────────────────────────────


class TestConfigResolution:
    def _load(self, tmp_path, monkeypatch, cfg: dict):
        cfg_path = tmp_path / "config.json"
        cfg.setdefault("_config_version", 3)  # skip migration rewriting
        cfg_path.write_text(json.dumps(cfg))
        monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
        # Keep the real runtime env file out of the test.
        monkeypatch.setenv("CLAIRE_ENV_FILE", str(tmp_path / "absent.env"))
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        from claire.config import load_settings
        return load_settings()

    def test_gemini_provider_defaults_the_model(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "embedding_provider": "gemini",
            "embedding_model": "",
        })

        assert settings.embedding_provider == "gemini"
        assert settings.embedding_model == "gemini-embedding-2"

    def test_ollama_model_default_is_unchanged(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "embedding_provider": "ollama",
            "embedding_model": "",
        })

        assert settings.embedding_model == "nomic-embed-text"

    def test_explicit_gemini_model_is_kept(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "embedding_provider": "gemini",
            "embedding_model": "gemini-embedding-001",
        })

        assert settings.embedding_model == "gemini-embedding-001"

    def test_google_spelling_is_canonicalised(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "embedding_provider": "google",
            "embedding_model": "",
        })

        assert settings.embedding_provider == "gemini"

    def test_gemini_key_is_read_from_the_environment_not_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", _STUB_KEY)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "_config_version": 3,
            "embedding_provider": "gemini",
            "embedding_model": "",
        }))
        monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
        monkeypatch.setenv("CLAIRE_ENV_FILE", str(tmp_path / "absent.env"))
        from claire.config import load_settings

        settings = load_settings()

        assert settings.embedding_api_key == _STUB_KEY
        # The value must not have been written back into the config file.
        assert _STUB_KEY not in cfg_path.read_text()

    def test_unknown_provider_is_still_rejected(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "embedding_provider": "anthropic",
            "embedding_model": "",
        })

        assert settings.embedding_provider == ""


# ── memory call-site plumbing ─────────────────────────────────────────────


class TestMemoryCallSites:
    def test_embed_text_forwards_the_task_type(self, monkeypatch):
        from claire.llm import TASK_RETRIEVAL_DOCUMENT
        from claire.memory import conversation

        seen = {}

        class _Backend:
            def embed(self, text, model, timeout_sec=15.0, task_type=None):
                seen.update(text=text, model=model, task_type=task_type)
                return _vector()

        monkeypatch.setattr(conversation, "get_embedding_backend", lambda cfg: _Backend())

        result = conversation._embed_text(
            "a memory", _cfg(), task_type=TASK_RETRIEVAL_DOCUMENT
        )

        assert result is not None
        assert seen["task_type"] == TASK_RETRIEVAL_DOCUMENT
        assert seen["model"] == "gemini-embedding-2"

    def test_embed_text_returns_none_when_the_backend_raises(self, monkeypatch):
        from claire.memory import conversation

        class _Backend:
            def embed(self, *args, **kwargs):
                raise RuntimeError("no key")

        monkeypatch.setattr(conversation, "get_embedding_backend", lambda cfg: _Backend())

        assert conversation._embed_text("a memory", _cfg()) is None

    def test_indexing_uses_document_and_search_uses_query(self, monkeypatch):
        """The two sides of retrieval must not share a task type."""
        from claire.llm import TASK_RETRIEVAL_DOCUMENT, TASK_RETRIEVAL_QUERY
        from claire.memory import conversation

        recorded = []

        class _Backend:
            def embed(self, text, model, timeout_sec=15.0, task_type=None):
                recorded.append(task_type)
                return _vector()

        monkeypatch.setattr(conversation, "get_embedding_backend", lambda cfg: _Backend())

        conversation._embed_text("indexed memory", _cfg(),
                                 task_type=TASK_RETRIEVAL_DOCUMENT)
        conversation._embed_text("search query", _cfg(),
                                 task_type=TASK_RETRIEVAL_QUERY)

        assert recorded == [TASK_RETRIEVAL_DOCUMENT, TASK_RETRIEVAL_QUERY]

    def test_a_failed_embed_leaves_the_index_untouched_and_keywords_work(
        self, tmp_path, monkeypatch
    ):
        """The keyword fallback must survive a dead embedding service, and a
        failed embed must not leave a half-written vector behind."""
        from claire.memory import conversation
        from claire.memory.db import Database
        from claire.utils.vector_store import PythonVectorStore

        monkeypatch.setattr(
            "claire.utils.vector_store.get_best_vector_store",
            lambda path, dimension: PythonVectorStore(path),
        )
        db = Database(str(tmp_path / "diary.db"))
        try:
            ident = db.upsert_conversation_summary(
                "2026-01-01", "The user prefers Celsius over Fahrenheit.", "preferences"
            )
            assert ident is not None
            store = db._python_vector_store
            before = len(store.vectors)

            class _Dead:
                def embed(self, *args, **kwargs):
                    return None

            monkeypatch.setattr(conversation, "get_embedding_backend", lambda cfg: _Dead())
            vec = conversation._embed_text("The user prefers Celsius.", _cfg())
            assert vec is None
            # Nothing was written for the failed embed.
            assert len(store.vectors) == before

            hits = db.search_hybrid("Celsius", None, top_k=5)
            assert any(row["id"] == ident for row in hits)
        finally:
            db.close()
