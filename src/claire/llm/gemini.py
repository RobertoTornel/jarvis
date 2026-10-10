"""Google Gemini embeddings, over the native ``generativelanguage`` API.

Why a dedicated backend and not the OpenAI-compatible shim
----------------------------------------------------------
Google exposes both a native surface (``:embedContent`` /
``:batchEmbedContents``) and an OpenAI-compatible one
(``/v1beta/openai/embeddings``).  This module uses the **native** surface,
for three reasons that all matter to Claire's memory search:

1. **Asymmetric task types.**  Retrieval works measurably better when the
   indexed document and the search query are embedded *for their own side*
   of the retrieval (``RETRIEVAL_DOCUMENT`` vs ``RETRIEVAL_QUERY``).  That is
   a native request field; the OpenAI embeddings schema has no slot for it,
   so on the compatible surface the hint would be dropped or, worse, a
   strict server would 400 on the unknown key.
2. **A pinned output length.**  The local index is fixed at 768 floats (see
   :mod:`claire.utils.embedding_vector`), and ``outputDimensionality`` is a
   native field; the compatible surface does not document an equivalent.
3. **Real batching.**  ``batchEmbedContents`` embeds many memories in one
   request instead of one round-trip per memory.

Model choice
------------
Contract: ``gemini-embedding-2`` is the default (the owner asked for the
newest model; the id they gave, ``gemini-embedding-002``, does not exist).
``gemini-embedding-001`` is the text-only alternative and is a first-class
config value — set ``embedding_model``.  Both are stable, both are
Matryoshka-capable, both default to 3072 dims and both accept
``outputDimensionality: 768``; Google documents their **embedding spaces as
incompatible with each other**, so switching between them is a re-index, not
a config flip.

Task types differ between the two families and this module handles both:
Embedding 1 takes a native ``taskType`` field; Embedding 2 does not, and
Google's documented substitute is a task *prefix* in the text itself.  The
prefix format below follows the "Retrieval use cases (Asymmetric format)"
table in https://ai.google.dev/gemini-api/docs/embeddings.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import requests

from ..debug import debug_log
from ..utils.embedding_vector import EMBEDDING_DIMENSION
from .backend import (
    LLMBackend,
    TASK_RETRIEVAL_DOCUMENT,
    TASK_RETRIEVAL_QUERY,
)

#: Provider id used in ``embedding_provider`` / ``llm_provider``.
GEMINI_PROVIDER = "gemini"

#: Native REST base.  A custom value (a proxy, or a Vertex-compatible
#: gateway) can be supplied through ``embedding_base_url``.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: Default embedding model.  See the module docstring for the justification
#: and for the text-only alternative, ``gemini-embedding-001``.
GEMINI_DEFAULT_EMBED_MODEL = "gemini-embedding-2"

#: The text-only alternative, kept as a named constant so config resolution,
#: docs and tests all spell it the same way.
GEMINI_TEXT_ONLY_EMBED_MODEL = "gemini-embedding-001"

#: Output length written to the index.  This is the store's existing
#: dimension, not a free choice — see :mod:`claire.utils.embedding_vector`.
GEMINI_OUTPUT_DIMENSION = EMBEDDING_DIMENSION

#: Task types the native API documents for embedding-001.  An unknown value
#: is dropped rather than sent, so a typo cannot become a 400.
_NATIVE_TASK_TYPES = frozenset({
    "RETRIEVAL_QUERY",
    "RETRIEVAL_DOCUMENT",
    "SEMANTIC_SIMILARITY",
    "CLASSIFICATION",
    "CLUSTERING",
    "QUESTION_ANSWERING",
    "FACT_VERIFICATION",
    "CODE_RETRIEVAL_QUERY",
})

#: ``gemini-embedding-2`` replaces ``taskType`` with a prefix in the text.
#: Only the documented pairs are mapped; anything else is sent unprefixed.
_V2_QUERY_PREFIXES = {
    TASK_RETRIEVAL_QUERY: "task: search result | query: ",
    "QUESTION_ANSWERING": "task: question answering | query: ",
    "FACT_VERIFICATION": "task: fact checking | query: ",
    "CODE_RETRIEVAL_QUERY": "task: code retrieval | query: ",
}

#: ``batchEmbedContents`` caps a request at 100 inputs; longer runs are split.
_BATCH_LIMIT = 100


def uses_task_prefixes(model: str) -> bool:
    """Whether ``model`` is an Embedding 2 family model.

    Embedding 2 does not take ``taskType``; it takes a task prefix.  Any
    other id (``gemini-embedding-001``, ``text-embedding-004``, a future
    sibling) keeps the native field.
    """
    return "embedding-2" in (model or "").lower()


def format_for_task(text: str, model: str, task_type: Optional[str]) -> str:
    """Apply Embedding 2's documented task prefix, or return ``text``.

    Embedding 1 needs no transformation: its task type travels in the body.
    """
    if not task_type or not uses_task_prefixes(model):
        return text
    prefix = _V2_QUERY_PREFIXES.get(task_type)
    if prefix is not None:
        return f"{prefix}{text}"
    if task_type == TASK_RETRIEVAL_DOCUMENT:
        # No title is available for a memory summary; the docs say to use
        # the literal "none" when a document has no title.
        return f"title: none | text: {text}"
    # Classification / clustering / similarity are symmetric: no prefix.
    return text


def _native_task_type(model: str, task_type: Optional[str]) -> Optional[str]:
    if not task_type or uses_task_prefixes(model):
        return None
    return task_type if task_type in _NATIVE_TASK_TYPES else None


class GeminiEmbeddingBackend(LLMBackend):
    """Embeddings-only backend for the Gemini API.

    Chat methods are deliberately unimplemented: ``get_llm_backend`` never
    builds this class, and a silent empty chat answer would be worse than a
    loud error if anything ever did.
    """

    def __init__(self, base_url: str = GEMINI_BASE_URL, api_key: Optional[str] = None) -> None:
        self.base_url = (base_url or GEMINI_BASE_URL).rstrip("/")
        # Never logged, never echoed; empty means "no key configured" and is
        # reported as a failed embed, not as an anonymous request.
        self._api_key = api_key or None

    # ── embeddings ────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            # Google's documented header.  The query-string alternative
            # (``?key=``) would put the secret in every URL and therefore in
            # every log and proxy trace, so it is not used.
            headers["x-goog-api-key"] = self._api_key
        return headers

    def _request_payload(self, text: str, model: str, task_type: Optional[str]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": f"models/{model}",
            "content": {"parts": [{"text": format_for_task(text, model, task_type)}]},
            "outputDimensionality": GEMINI_OUTPUT_DIMENSION,
        }
        native = _native_task_type(model, task_type)
        if native is not None:
            payload["taskType"] = native
        return payload

    @staticmethod
    def _values_from_embedding(entry: Any) -> Optional[List[float]]:
        values = entry.get("values") if isinstance(entry, dict) else None
        if not isinstance(values, list) or not values:
            return None
        try:
            return [float(x) for x in values]
        except (TypeError, ValueError):
            return None

    def _accept(self, values: Optional[List[float]], model: str) -> Optional[List[float]]:
        """Return ``values`` only if they fit the index.

        A wrong-length vector is never returned to a caller: the index is
        fixed at :data:`GEMINI_OUTPUT_DIMENSION` floats, and passing the
        bad vector on would push the problem into storage.  The caller sees
        ``None``, logs the reason, and the keyword fallback takes over.
        """
        if values is None:
            debug_log(
                f"Gemini embedding for {model!r} came back with no usable vector",
                "llm",
            )
            return None
        if len(values) != GEMINI_OUTPUT_DIMENSION:
            debug_log(
                f"Gemini embedding for {model!r} returned {len(values)} dimensions "
                f"but the index is fixed at {GEMINI_OUTPUT_DIMENSION}; discarding it",
                "llm",
            )
            return None
        return values

    def embed(
        self,
        text: str,
        model: str,
        timeout_sec: float = 15.0,
        task_type: Optional[str] = None,
    ) -> Optional[List[float]]:
        """Embed one text via ``:embedContent``."""
        if not self._api_key:
            debug_log(
                "Gemini embeddings are configured but no API key is present "
                "(set embedding_api_key or GEMINI_API_KEY); skipping the request",
                "llm",
            )
            return None
        try:
            resp = requests.post(
                f"{self.base_url}/models/{model}:embedContent",
                json=self._request_payload(text, model, task_type),
                headers=self._headers(),
                timeout=timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            # Only the class and status are logged: an exception string can
            # carry the URL, and the URL is where a key would leak if the
            # query-string form were ever used.
            debug_log(
                f"Gemini embedContent failed ({type(exc).__name__}); "
                "memory search will fall back to keywords",
                "llm",
            )
            return None
        entry = data.get("embedding") if isinstance(data, dict) else None
        return self._accept(self._values_from_embedding(entry), model)

    def embed_batch(
        self,
        texts: List[str],
        model: str,
        timeout_sec: float = 15.0,
        task_type: Optional[str] = None,
    ) -> List[Optional[List[float]]]:
        """Embed many texts via ``:batchEmbedContents``.

        Returns one entry per input, in order, with ``None`` for any input
        that failed or came back the wrong length.  The batch result is
        positional, so a malformed response degrades to all-``None`` rather
        than silently shifting vectors onto the wrong memories.
        """
        results: List[Optional[List[float]]] = [None] * len(texts)
        if not texts:
            return results
        if not self._api_key:
            debug_log(
                "Gemini embeddings are configured but no API key is present "
                "(set embedding_api_key or GEMINI_API_KEY); skipping the request",
                "llm",
            )
            return results
        for start in range(0, len(texts), _BATCH_LIMIT):
            chunk = texts[start:start + _BATCH_LIMIT]
            try:
                resp = requests.post(
                    f"{self.base_url}/models/{model}:batchEmbedContents",
                    json={"requests": [
                        self._request_payload(text, model, task_type) for text in chunk
                    ]},
                    headers=self._headers(),
                    timeout=timeout_sec,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                debug_log(
                    f"Gemini batchEmbedContents failed ({type(exc).__name__}); "
                    "those memories keep keyword-searchable text only",
                    "llm",
                )
                continue
            entries = data.get("embeddings") if isinstance(data, dict) else None
            if not isinstance(entries, list) or len(entries) != len(chunk):
                debug_log(
                    "Gemini batchEmbedContents returned a result set that does "
                    f"not line up with the {len(chunk)} inputs sent; discarding it",
                    "llm",
                )
                continue
            for offset, entry in enumerate(entries):
                results[start + offset] = self._accept(
                    self._values_from_embedding(entry), model
                )
        return results

    def list_models(self, timeout_sec: float = 5.0) -> List[str]:
        """Named for the backend interface; discovery is not needed here.

        Listing every model on the account would need a different endpoint
        and a configured key, and nothing in Claire asks the embedding
        backend to enumerate models.
        """
        return []

    # ── chat is not this backend's job ────────────────────────────────

    def _chat_unsupported(self) -> None:
        raise NotImplementedError(
            "GeminiEmbeddingBackend serves embeddings only; "
            "get_llm_backend() never returns it"
        )

    def direct(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        timeout_sec: float = 10.0,
        thinking: bool = False,
        num_ctx: int = 4096,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        self._chat_unsupported()

    def streaming(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        on_token: Optional[Callable[[str], None]] = None,
        timeout_sec: float = 30.0,
        thinking: bool = False,
    ) -> Optional[str]:
        self._chat_unsupported()

    def chat(
        self,
        chat_model: str,
        messages: List[Dict[str, Any]],
        timeout_sec: float = 30.0,
        extra_options: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        thinking: bool = False,
    ) -> Optional[Dict[str, Any]]:
        self._chat_unsupported()
