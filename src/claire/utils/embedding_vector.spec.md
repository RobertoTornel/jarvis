# `claire/utils/embedding_vector.py` — the embedding dimension contract

## Responsibility

One job: turn a raw embedding into the unit vector the local index stores, or
refuse it. The module also owns the constant that defines how wide that index
is.

## Interface

| Name | Meaning |
|---|---|
| `EMBEDDING_DIMENSION` | `768`. The width of every vector the index accepts. |
| `normalise_embedding(vector, dimension=None)` | returns a finite `float32` unit vector; raises `ValueError` for a non-numeric, empty, non-finite, zero-direction or wrong-length input |

## Why 768 is a constant and not a config key

The width is baked into two storage formats that cannot be migrated in place:

- `sqlite_vss_path` installs: `CREATE VIRTUAL TABLE embeddings USING
  vss0(vec(768) ...)` (`claire/memory/db.py`).
- Otherwise: the FAISS index is built at `dimension=768`, or the pure-Python
  store is used — and that one is width-agnostic, skipping mismatched pairs at
  search time (`claire/utils/vector_store.py`).

An embedding whose length does not match is worse than no embedding: FAISS
rejects it and sqlite-vss would hold a row the distance query cannot read back.
So the writers are pinned instead of the reader being made flexible:

1. `GeminiEmbeddingBackend` requests `outputDimensionality: 768` and discards
   any vector that comes back a different length (`claire/llm/gemini.py`).
2. `Database` builds its fallback store with `dimension=EMBEDDING_DIMENSION`.
3. `normalise_embedding(vector, store.dimension)` rejects a mismatch before any
   FAISS write; sqlite-vss rejects it through the declared column type.

Nothing calls `normalise_embedding` with a dimension of its own choosing; the
value always comes from the store.

## Invariants

1. The returned array is 1-D, `float32`, finite, and unit-norm (so cosine
   similarity is a dot product).
2. Scaling by the max magnitude happens before the norm, so a vector near
   `float64` range neither overflows nor underflows.
3. An unusable vector raises `ValueError`; callers turn that into a `None`
   embedding and the keyword fallback. Nothing partial is persisted.

## Not configurable, but a model change is still a re-index

Matching width does not mean matching space: Google documents
`gemini-embedding-001` and `gemini-embedding-2` as living in incompatible
embedding spaces. Changing `embedding_model` therefore requires re-embedding
existing memories even though both produce 768 floats. That is a data decision,
not a schema one.

## Tests

`tests/test_vector_validation.py`, `tests/test_hybrid_memory_search.py`,
`tests/test_gemini_embeddings.py` (the dimension-pinning tests), and
`tests/test_diary_embedding_failure.py` (the keyword fallback).
