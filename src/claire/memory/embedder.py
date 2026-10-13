"""Single source of embeddings for the Postgres + pgvector memory backend.

There is deliberately **no HTTP client in this module**. It reuses the configured
LLM embedding backend (``claire.llm.get_embedding_backend``) so that vault memory,
the diary and the listener all embed through exactly one code path and one setting
(``settings.embedding_model``). That matters because a second, parallel embedding
path could silently produce vectors from a different model, and two embedding
models with the same dimension still live in incomparable vector spaces.

The returned value is a :class:`pgvector.Vector`, never a bare ``list``: psycopg
sends a plain list as ``double precision[]``, and PostgreSQL then fails with
``operator does not exist: vector <=> double precision[]``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

from pgvector import Vector

LOGGER = logging.getLogger(__name__)

#: A spoken turn must never block on a cold embedding. Cold start was measured at
#: 2.18 s, warm at 6-15 ms, so the per-turn budget is clamped hard.
DEFAULT_TIMEOUT_SEC = 1.0

#: Dimension produced by the configured model (``nomic-embed-text``).
EMBEDDING_DIM = 768

_lock = threading.Lock()
_cached_settings: Any = None


def _debug(message: str) -> None:
    """Log through the project's debug channel when it is available."""
    try:
        from ..utils.debug import debug_log

        debug_log(message, "memory")
    except Exception:  # pragma: no cover - debug channel is optional
        LOGGER.debug(message)


def get_settings() -> Any:
    """Load and cache the runtime settings object."""
    global _cached_settings
    if _cached_settings is None:
        with _lock:
            if _cached_settings is None:
                from ..config import load_settings

                _cached_settings = load_settings()
    return _cached_settings


def set_settings(settings: Any) -> None:
    """Override the cached settings (used by tests)."""
    global _cached_settings
    with _lock:
        _cached_settings = settings


def model_name(settings: Any = None) -> str:
    """The configured embedding model, never a hardcoded literal as the source."""
    settings = settings if settings is not None else get_settings()
    return str(getattr(settings, "embedding_model", "") or "").strip() or "nomic-embed-text"


def provider_name(settings: Any = None) -> str:
    """The provider that actually serves embeddings.

    ``embedding_provider`` of ``""`` means "same as ``llm_provider``", which is
    exactly the inheritance that makes a Gemini switch dangerous. Record it.
    """
    settings = settings if settings is not None else get_settings()
    raw = str(getattr(settings, "embedding_provider", "") or "").strip()
    if raw:
        return raw
    return str(getattr(settings, "llm_provider", "") or "").strip() or "ollama"


def embed_text(
    text: str,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    task_type: Optional[str] = None,
    settings: Any = None,
) -> Optional[Vector]:
    """Embed *text* and return a :class:`Vector`, or ``None`` on failure.

    Failure is returned rather than raised so that callers can degrade to keyword
    search instead of failing a turn. It is never silent: the failure is logged.
    """
    text = text or ""
    if not text.strip():
        return None

    settings = settings if settings is not None else get_settings()

    try:
        from ..llm import get_embedding_backend
    except Exception as e:  # pragma: no cover
        _debug(f"⚠️ embedding backend unavailable ({type(e).__name__})")
        return None

    try:
        backend = get_embedding_backend(settings)
        raw: Optional[List[float]] = backend.embed(
            text,
            model_name(settings),
            timeout_sec=float(timeout_sec),
            task_type=task_type,
        )
    except Exception as e:
        _debug(
            f"⚠️ embedding failed ({type(e).__name__}: {e}); "
            "memory search falls back to keyword search"
        )
        return None

    if not raw:
        _debug("⚠️ embedding returned nothing; falling back to keyword search")
        return None

    if len(raw) != EMBEDDING_DIM:
        # A different model would put rows in an incomparable space. Refuse loudly.
        _debug(
            f"⚠️ embedding dimension {len(raw)} != {EMBEDDING_DIM}; refusing to store"
        )
        return None

    return Vector(raw)


def embed_document(text: str, **kw: Any) -> Optional[Vector]:
    """Embed text destined to be *stored* (asymmetric-retrieval hint)."""
    try:
        from ..llm.backend import TASK_RETRIEVAL_DOCUMENT

        kw.setdefault("task_type", TASK_RETRIEVAL_DOCUMENT)
    except Exception:  # pragma: no cover
        pass
    return embed_text(text, **kw)


def embed_query(text: str, **kw: Any) -> Optional[Vector]:
    """Embed a *search query* (asymmetric-retrieval hint)."""
    try:
        from ..llm.backend import TASK_RETRIEVAL_QUERY

        kw.setdefault("task_type", TASK_RETRIEVAL_QUERY)
    except Exception:  # pragma: no cover
        pass
    return embed_text(text, **kw)
