"""Factories for resolving the active LLM and embedding backends.

Two factories share one provider catalogue:

- :func:`get_llm_backend` — chat / completion path. Dispatches on
  ``settings.llm_provider``.
- :func:`get_embedding_backend` — embeddings path. Dispatches on
  ``settings.embedding_provider``, falling back to the LLM provider
  when unset. The override exists for runtimes that ship chat without
  embeddings (early oMLX builds, some llama.cpp configurations); users
  can keep chat on their preferred runtime and route embeddings
  through Ollama instead.

Gemini is an **embeddings-only** provider, so it is deliberately absent from
``get_llm_backend``: ``"gemini"`` in ``llm_provider`` keeps resolving to
Ollama exactly as any other unknown chat provider does. Put it in
``embedding_provider`` to route memory-search embeddings to Google:

.. code-block:: json

    {
      "embedding_provider": "gemini",
      "embedding_model": "gemini-embedding-2",
      "embedding_api_key": ""
    }

An empty ``embedding_api_key`` falls back to ``GEMINI_API_KEY`` (then
``GOOGLE_API_KEY``) in the environment — the same shape as the OmniRouter
chat key, which is read from the environment rather than stored.
"""

from __future__ import annotations
import os
from typing import Any, Optional

from .backend import LLMBackend
from .gemini import (
    GEMINI_BASE_URL,
    GEMINI_PROVIDER,
    GeminiEmbeddingBackend,
)
from .ollama import OllamaBackend
from .openai_compatible import OpenAICompatibleBackend


_OLLAMA = "ollama"
_OPENAI_COMPATIBLE = "openai_compatible"
_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

#: Environment variables checked, in order, when ``embedding_api_key`` is
#: empty and the embedding provider is Gemini.  Two names because Google's
#: own SDKs and examples use both.
_GEMINI_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

#: Accepted spellings of the Gemini provider.  ``google`` is the spelling
#: people reach for when they think of the vendor rather than the product;
#: ``claire.config`` normalises ``embedding_provider`` to ``"gemini"`` and
#: rewrites ``"google"`` to it, but this factory is also called with raw
#: settings objects, so it keeps that spelling.  ``"google_gemini"`` is not
#: here: config's allow-list rejects it before it can reach this alias set,
#: and nothing else names it.
_GEMINI_ALIASES = frozenset({GEMINI_PROVIDER, "google"})


def _resolve_provider(value: Any) -> str:
    if isinstance(value, str):
        v = value.strip().lower()
        if v == _OPENAI_COMPATIBLE:
            return _OPENAI_COMPATIBLE
    return _OLLAMA


def _resolve_embedding_provider(value: Any) -> str:
    """Embedding-provider resolution, which knows Gemini as well.

    Kept separate from :func:`_resolve_provider` so adding an embedding-only
    provider can never change which chat backend a string resolves to.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _GEMINI_ALIASES:
            return GEMINI_PROVIDER
    return _resolve_provider(value)


def gemini_env_api_key() -> str:
    """Return ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` from the environment.

    The value is returned to the backend and never logged.
    """
    for name in _GEMINI_KEY_ENV_VARS:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def _str_attr(settings: Any, name: str, default: str = "") -> str:
    val = getattr(settings, name, None)
    return val if isinstance(val, str) and val else default


def _build(provider: str, base_url: str, api_key: Optional[str]) -> LLMBackend:
    if provider == _OPENAI_COMPATIBLE:
        return OpenAICompatibleBackend(base_url, api_key=api_key)
    return OllamaBackend(base_url)


def get_llm_backend(settings: Any) -> LLMBackend:
    """Return the configured chat backend.

    ``llm_base_url`` is the OpenAI-compatible server's URL; the Ollama path
    uses ``ollama_base_url``. Keeping each provider on its own URL field
    means toggling ``llm_provider`` back to Ollama can never leave the
    backend pointed at a stale OpenAI-compatible URL.
    """
    provider = _resolve_provider(getattr(settings, "llm_provider", None))
    if provider == _OPENAI_COMPATIBLE:
        base_url = _str_attr(settings, "llm_base_url") or _str_attr(
            settings, "ollama_base_url", _DEFAULT_OLLAMA_URL
        )
    else:
        base_url = _str_attr(settings, "ollama_base_url", _DEFAULT_OLLAMA_URL)
    api_key = _str_attr(settings, "llm_api_key") or None
    return _build(provider, base_url, api_key)


def get_embedding_backend(settings: Any) -> LLMBackend:
    """Return the configured embedding backend.

    Falls through ``embedding_provider`` → ``llm_provider`` → ``"ollama"``.
    Users can pin embeddings to Ollama (recommended when chat runs on a
    runtime without embedding support) by setting
    ``embedding_provider: "ollama"`` in their config, or route them to
    Google's Gemini API with ``embedding_provider: "gemini"``.

    The Gemini branch never inherits ``llm_api_key``: that key belongs to the
    chat router, and sending it to Google would be both useless and a leak.
    It uses ``embedding_api_key`` or the ``GEMINI_API_KEY`` /
    ``GOOGLE_API_KEY`` environment variables.
    """
    raw = getattr(settings, "embedding_provider", None)
    if isinstance(raw, str) and raw.strip():
        provider = _resolve_embedding_provider(raw)
    else:
        provider = _resolve_provider(getattr(settings, "llm_provider", None))

    if provider == GEMINI_PROVIDER:
        base_url = _str_attr(settings, "embedding_base_url") or GEMINI_BASE_URL
        api_key = _str_attr(settings, "embedding_api_key") or gemini_env_api_key() or None
        return GeminiEmbeddingBackend(base_url, api_key=api_key)

    base_url = _str_attr(settings, "embedding_base_url")
    if not base_url:
        if provider == _OPENAI_COMPATIBLE:
            base_url = _str_attr(settings, "llm_base_url")
        else:
            base_url = _str_attr(settings, "ollama_base_url", _DEFAULT_OLLAMA_URL)
    if not base_url:
        base_url = _DEFAULT_OLLAMA_URL

    api_key = _str_attr(settings, "embedding_api_key") or _str_attr(
        settings, "llm_api_key"
    ) or None
    return _build(provider, base_url, api_key)
