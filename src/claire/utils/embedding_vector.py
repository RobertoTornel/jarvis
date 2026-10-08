"""Validate and normalise local semantic-index vectors.

The dimension contract
----------------------
Every vector that reaches a Claire index is length :data:`EMBEDDING_DIMENSION`.
That number is not a preference; it is baked into two storage formats:

* the sqlite-vss virtual table is declared ``vss0(vec(768))``
  (``claire/memory/db.py``), and
* the FAISS/python fallback index is built with ``dimension=768``
  (``claire/utils/fast_vector_store.py``, reached from
  ``Database.__init__``).

Neither format is migratable in place, and a vector of the wrong length is
worse than no vector at all: FAISS rejects it, and sqlite-vss would store a
row the distance query can never read back correctly.  So rather than making
the dimension configurable, embedding backends **pin their output to this
constant** — see :data:`claire.llm.gemini.GEMINI_OUTPUT_DIMENSION` — and
every write path validates against it before touching storage.

A model change is still a re-index, not a re-dimension: two models that agree
on length (for example ``gemini-embedding-001`` and ``gemini-embedding-2``,
whose embedding spaces Google documents as incompatible) still do not produce
comparable vectors. Switching models requires re-embedding existing memories,
which is a data decision, not a schema one.
"""
import numpy as np


#: Length of every vector in the local semantic index. See the module docstring.
EMBEDDING_DIMENSION = 768


def normalise_embedding(vector, dimension=None) -> np.ndarray:
    """Return a finite unit vector, rejecting unusable embeddings."""
    try:
        values = np.asarray(vector, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Embedding must contain numeric values') from exc
    if values.ndim != 1 or not values.size or not np.isfinite(values).all():
        raise ValueError('Embedding must be a non-empty finite vector')
    if dimension is not None and values.size != dimension:
        raise ValueError('Embedding dimension does not match the index')
    scale = np.max(np.abs(values))
    if scale == 0:
        raise ValueError('Embedding must have a non-zero direction')
    # Scaling first avoids overflow and underflow in the squared norm.
    values = values / scale
    return (values / np.linalg.norm(values)).astype(np.float32)
