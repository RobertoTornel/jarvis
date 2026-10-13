"""PostgreSQL + pgvector storage for Claire's long-term memory.

This module is the storage layer only. It does not decide *when* memory is read;
that is the callers' job. See ``docs/PGVECTOR_DESIGN.md`` for the schema rationale.

Two properties are load-bearing:

* **Importing this module must not connect.** A machine with no database still
  imports Claire; the failure surfaces as :class:`MemoryStoreUnavailable` at the
  point of use, with a message that names the fix.
* **Every connection registers the pgvector adapter.** Without it psycopg binds a
  Python list as ``double precision[]`` and PostgreSQL rejects the cosine operator.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

LOGGER = logging.getLogger(__name__)

EMBEDDING_DIM = 768
SCHEMA_VERSION = 2

DEFAULT_ENV_FILE = "~/.config/claire/pg.env"
DEFAULT_DB = "claire"


class MemoryStoreError(RuntimeError):
    """Any failure originating in the memory store."""


class MemoryStoreUnavailable(MemoryStoreError):
    """The store cannot be reached at all (no DSN, refused connection)."""


class MemoryStoreSchemaError(MemoryStoreError):
    """The schema is missing or could not be created."""


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer      PRIMARY KEY,
    name        text         NOT NULL,
    checksum    text         NOT NULL,
    applied_at  timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_documents (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_path    text        NOT NULL UNIQUE,
    doc_kind       text        NOT NULL CHECK (doc_kind IN ('router','note')),
    name           text        NOT NULL,
    title          text        NOT NULL DEFAULT '',
    body           text        NOT NULL,
    frontmatter    jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
    always_loaded  boolean     NOT NULL DEFAULT false,
    budget_bytes   integer,
    content_sha256 text        NOT NULL,
    source_mtime   timestamptz,
    source_bytes   integer,
    imported_at    timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- Idempotent upgrade for databases created before frontmatter_raw existed.
ALTER TABLE memory_documents ADD COLUMN IF NOT EXISTS frontmatter_raw text NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS memory_documents_one_router_idx
    ON memory_documents (doc_kind) WHERE doc_kind = 'router';

CREATE TABLE IF NOT EXISTS memory_chunks (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id    bigint      NOT NULL REFERENCES memory_documents(id) ON DELETE CASCADE,
    chunk_index    integer     NOT NULL,
    heading        text        NOT NULL DEFAULT '',
    content        text        NOT NULL,
    char_start     integer     NOT NULL,
    char_end       integer     NOT NULL,
    token_estimate integer     NOT NULL,
    content_sha256 text        NOT NULL,
    embedding      vector({EMBEDDING_DIM}),
    embed_model    text,
    embed_provider text,
    embedded_at    timestamptz,
    tsv            tsvector    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS memory_chunks_embedding_hnsw_idx
    ON memory_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS memory_chunks_pending_embedding_idx
    ON memory_chunks (document_id, chunk_index) WHERE embedding IS NULL;

CREATE INDEX IF NOT EXISTS memory_chunks_tsv_idx ON memory_chunks USING gin (tsv);

CREATE INDEX IF NOT EXISTS memory_chunks_document_id_idx ON memory_chunks (document_id);
"""


def ddl_checksum() -> str:
    return hashlib.sha256(DDL.encode("utf-8")).hexdigest()


def as_vector(value: Any) -> Optional[Vector]:
    """Coerce a list/ndarray/Vector into a :class:`Vector` (idempotent)."""
    if value is None:
        return None
    if isinstance(value, Vector):
        return value
    return Vector(list(value))


# ---------------------------------------------------------------------------
# DSN resolution
# ---------------------------------------------------------------------------

def read_env_file(path: Path) -> Dict[str, str]:
    """Parse a ``KEY=VALUE`` env file. Missing or unreadable files yield ``{}``."""
    values: Dict[str, str] = {}
    try:
        raw = path.expanduser().read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def resolve_dsn(explicit: Optional[str] = None, *, database: Optional[str] = None) -> str:
    """Resolve the Postgres DSN.

    Order: explicit argument, ``CLAIRE_PG_DSN`` in the environment, then
    ``CLAIRE_PG_DSN`` inside ``~/.config/claire/pg.env``, then assembled from the
    ``CLAIRE_PG_*`` parts. The password is never logged.
    """
    if explicit:
        return explicit

    env_dsn = (os.environ.get("CLAIRE_PG_DSN") or "").strip()
    if env_dsn:
        return env_dsn

    # config.json's `pg_dsn`, when set, wins over the env file but not over the
    # environment. Imported lazily so this module stays import-cheap.
    try:
        from ..config import load_settings

        configured = str(getattr(load_settings(), "pg_dsn", "") or "").strip()
        if configured:
            return configured
    except Exception:
        pass

    env_path = Path(
        os.environ.get("CLAIRE_PG_ENV_FILE", DEFAULT_ENV_FILE)
    ).expanduser()
    values = read_env_file(env_path)

    if database:
        test = (values.get("CLAIRE_PG_TEST_DSN") or "").strip()
        if database != DEFAULT_DB and test:
            return test

    file_dsn = (values.get("CLAIRE_PG_DSN") or "").strip()
    if file_dsn:
        return file_dsn

    user = values.get("CLAIRE_PG_USER") or os.environ.get("CLAIRE_PG_USER")
    password = values.get("CLAIRE_PG_PASSWORD") or os.environ.get("CLAIRE_PG_PASSWORD")
    host = values.get("CLAIRE_PG_HOST") or os.environ.get("CLAIRE_PG_HOST") or "127.0.0.1"
    port = values.get("CLAIRE_PG_PORT") or os.environ.get("CLAIRE_PG_PORT") or "5432"
    db = database or values.get("CLAIRE_PG_DB") or DEFAULT_DB

    if user and password:
        return (
            f"postgresql://{urllib.parse.quote(user, safe='')}:"
            f"{urllib.parse.quote(password, safe='')}@{host}:{port}/{db}"
        )

    raise MemoryStoreUnavailable(
        "No Postgres DSN found. Set CLAIRE_PG_DSN, or create "
        f"{DEFAULT_ENV_FILE} with CLAIRE_PG_DSN=(or CLAIRE_PG_USER/"
        "CLAIRE_PG_PASSWORD/CLAIRE_PG_HOST/CLAIRE_PG_PORT/CLAIRE_PG_DB). "
        "See deploy/pgvector/README.md."
    )


def test_dsn() -> str:
    """The DSN tests and dry runs should use (the ``claire_test`` database)."""
    explicit = (os.environ.get("CLAIRE_PG_TEST_DSN") or "").strip()
    if explicit:
        return explicit
    values = read_env_file(Path(os.environ.get("CLAIRE_PG_ENV_FILE", DEFAULT_ENV_FILE)))
    test = (values.get("CLAIRE_PG_TEST_DSN") or "").strip()
    if test:
        return test
    return resolve_dsn(database="claire_test")


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def _configure(conn: "psycopg.Connection[Any]") -> None:
    """Per-connection setup: teach psycopg how to bind a ``vector``."""
    register_vector(conn)


_pool_lock = threading.Lock()
_pool: Any = None
_pool_dsn: Optional[str] = None


def get_pool(dsn: Optional[str] = None) -> Any:
    """Lazily create (and cache) the connection pool."""
    global _pool, _pool_dsn
    target = dsn or resolve_dsn()
    with _pool_lock:
        if _pool is not None and _pool_dsn == target:
            return _pool
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
        try:
            from psycopg_pool import ConnectionPool
        except Exception as e:  # pragma: no cover
            raise MemoryStoreUnavailable(
                "psycopg_pool is not installed; run "
                "`.venv/bin/pip install 'psycopg[binary,pool]'`."
            ) from e
        _pool = ConnectionPool(
            conninfo=target,
            min_size=1,
            max_size=4,
            configure=_configure,
            open=True,
            timeout=5.0,
        )
        _pool_dsn = target
        return _pool


def close_pool() -> None:
    """Close and forget the pool (used by tests and shutdown)."""
    global _pool, _pool_dsn
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
        _pool = None
        _pool_dsn = None


def connection(dsn: Optional[str] = None):
    """Yield a pooled connection, translating driver errors into our own."""
    try:
        pool = get_pool(dsn)
        return pool.connection()
    except MemoryStoreError:
        raise
    except Exception as e:
        raise MemoryStoreUnavailable(
            f"Cannot reach Postgres: {type(e).__name__}: {e}. "
            "Is the claire-pg container running? See deploy/pgvector/README.md."
        ) from e


def health(dsn: Optional[str] = None) -> Tuple[bool, str]:
    """Cheap reachability probe. Returns ``(ok, detail)`` and never raises."""
    try:
        with connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                row = cur.fetchone()
        if not row:
            return False, "connected, but the pgvector extension is not installed"
        return True, f"pgvector {row[0]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def bootstrap_schema(dsn: Optional[str] = None) -> bool:
    """Create the schema if absent. Idempotent; safe to call on every start.

    Returns ``True`` when this call applied version 1, ``False`` when it was
    already present.
    """
    checksum = ddl_checksum()
    try:
        with connection(dsn) as conn:
            try:
                with conn.cursor() as cur:
                    # The DDL is entirely IF NOT EXISTS, so it is safe to run
                    # before we know whether the ledger table exists yet.
                    cur.execute(DDL)
                    cur.execute(
                        "SELECT version FROM schema_migrations WHERE version = %s",
                        (SCHEMA_VERSION,),
                    )
                    already = cur.fetchone() is not None
                    if not already:
                        cur.execute(
                            "INSERT INTO schema_migrations (version, name, checksum) "
                            "VALUES (%s, %s, %s) ON CONFLICT (version) DO NOTHING",
                            (SCHEMA_VERSION, "pgvector_memory_v2", checksum),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return not already
    except MemoryStoreError:
        raise
    except Exception as e:
        raise MemoryStoreSchemaError(f"Cannot create the memory schema: {e}") from e


def reset_schema(dsn: Optional[str] = None) -> None:
    """Drop the memory tables. **Tests only** — never call against production."""
    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS memory_chunks CASCADE")
            cur.execute("DROP TABLE IF EXISTS memory_documents CASCADE")
            cur.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
        conn.commit()


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """One embeddable slice of a document."""

    index: int
    content: str
    heading: str = ""
    char_start: int = 0
    char_end: int = 0
    embedding: Optional[Vector] = None

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.content) // 4)


@dataclass
class Document:
    """A stored memory document."""

    id: int
    source_path: str
    doc_kind: str
    name: str
    title: str
    body: str
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    frontmatter_raw: str = ""
    always_loaded: bool = False
    budget_bytes: Optional[int] = None
    content_sha256: str = ""

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode("utf-8"))


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _row_to_document(row: Sequence[Any]) -> Document:
    return Document(
        id=row[0],
        source_path=row[1],
        doc_kind=row[2],
        name=row[3],
        title=row[4],
        body=row[5],
        frontmatter=row[6] or {},
        frontmatter_raw=row[7] or "",
        always_loaded=row[8],
        budget_bytes=row[9],
        content_sha256=row[10],
    )


_DOC_COLUMNS = (
    "id, source_path, doc_kind, name, title, body, frontmatter, frontmatter_raw, "
    "always_loaded, budget_bytes, content_sha256"
)


def upsert_document(
    *,
    source_path: str,
    doc_kind: str,
    name: str,
    body: str,
    title: str = "",
    frontmatter: Optional[Dict[str, Any]] = None,
    frontmatter_raw: str = "",
    always_loaded: bool = False,
    budget_bytes: Optional[int] = None,
    source_mtime: Optional[Any] = None,
    source_bytes: Optional[int] = None,
    chunks: Optional[Sequence[Chunk]] = None,
    dsn: Optional[str] = None,
) -> Tuple[int, bool]:
    """Insert or update a document (keyed on ``source_path``).

    Returns ``(document_id, inserted)``. Re-running with identical input changes
    nothing: ``chunks`` whose ``content_sha256`` is unchanged keep their existing
    embedding and are not re-embedded.
    """
    if doc_kind not in ("router", "note"):
        raise MemoryStoreError(f"doc_kind must be 'router' or 'note', got {doc_kind!r}")

    digest = body_sha256(body)
    import json as _json

    with connection(dsn) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO memory_documents
                        (source_path, doc_kind, name, title, body, frontmatter,
                         frontmatter_raw, always_loaded, budget_bytes, content_sha256,
                         source_mtime, source_bytes)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (source_path) DO UPDATE SET
                        doc_kind      = EXCLUDED.doc_kind,
                        name          = EXCLUDED.name,
                        title         = EXCLUDED.title,
                        body          = EXCLUDED.body,
                        frontmatter   = EXCLUDED.frontmatter,
                        frontmatter_raw = EXCLUDED.frontmatter_raw,
                        always_loaded = EXCLUDED.always_loaded,
                        budget_bytes  = EXCLUDED.budget_bytes,
                        content_sha256= EXCLUDED.content_sha256,
                        source_mtime  = EXCLUDED.source_mtime,
                        source_bytes  = EXCLUDED.source_bytes,
                        updated_at    = now()
                    RETURNING id, (xmax = 0) AS inserted
                    """,
                    (
                        source_path, doc_kind, name, title, body,
                        _json.dumps(frontmatter or {}),
                        frontmatter_raw or "",
                        always_loaded, budget_bytes, digest, source_mtime, source_bytes,
                    ),
                )
                doc_id, inserted = cur.fetchone()

                if chunks is not None:
                    _replace_chunks(cur, doc_id, chunks)
            conn.commit()
        except MemoryStoreError:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise MemoryStoreError(f"upsert_document({source_path!r}) failed: {e}") from e
    return doc_id, bool(inserted)


def _replace_chunks(cur: Any, doc_id: int, chunks: Sequence[Chunk]) -> None:
    """Upsert chunks, re-embedding only those whose content actually changed."""
    from . import embedder

    cur.execute(
        "SELECT chunk_index, content_sha256, (embedding IS NOT NULL) "
        "FROM memory_chunks WHERE document_id = %s",
        (doc_id,),
    )
    existing = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    model = embedder.model_name()
    provider = embedder.provider_name()

    for chunk in chunks:
        digest = chunk.content_sha256
        prior = existing.get(chunk.index)
        unchanged = bool(prior and prior[0] == digest and prior[1])

        vector = chunk.embedding
        if vector is None and not unchanged:
            vector = embedder.embed_document(chunk.content)

        if vector is not None:
            cur.execute(
                """
                INSERT INTO memory_chunks
                    (document_id, chunk_index, heading, content, char_start, char_end,
                     token_estimate, content_sha256, embedding, embed_model, embed_provider, embedded_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (document_id, chunk_index) DO UPDATE SET
                    heading        = EXCLUDED.heading,
                    content        = EXCLUDED.content,
                    char_start     = EXCLUDED.char_start,
                    char_end       = EXCLUDED.char_end,
                    token_estimate = EXCLUDED.token_estimate,
                    content_sha256 = EXCLUDED.content_sha256,
                    embedding      = EXCLUDED.embedding,
                    embed_model    = EXCLUDED.embed_model,
                    embed_provider = EXCLUDED.embed_provider,
                    embedded_at    = now()
                """,
                (
                    doc_id, chunk.index, chunk.heading, chunk.content,
                    chunk.char_start, chunk.char_end, chunk.token_estimate,
                    digest, as_vector(vector), model, provider,
                ),
            )
        else:
            # No embedding available (backend down): store the text, leave the
            # vector NULL so the keyword half still works and the row is queued.
            cur.execute(
                """
                INSERT INTO memory_chunks
                    (document_id, chunk_index, heading, content, char_start, char_end,
                     token_estimate, content_sha256)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (document_id, chunk_index) DO UPDATE SET
                    heading        = EXCLUDED.heading,
                    content        = EXCLUDED.content,
                    char_start     = EXCLUDED.char_start,
                    char_end       = EXCLUDED.char_end,
                    token_estimate = EXCLUDED.token_estimate,
                    content_sha256 = EXCLUDED.content_sha256
                """,
                (
                    doc_id, chunk.index, chunk.heading, chunk.content,
                    chunk.char_start, chunk.char_end, chunk.token_estimate, digest,
                ),
            )

    if chunks:
        cur.execute(
            "DELETE FROM memory_chunks WHERE document_id = %s AND chunk_index >= %s",
            (doc_id, len(chunks)),
        )
    else:
        cur.execute("DELETE FROM memory_chunks WHERE document_id = %s", (doc_id,))


def get_document(name_or_path: str, *, dsn: Optional[str] = None) -> Optional[Document]:
    """Fetch a document by note name, source path, or router name."""
    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_DOC_COLUMNS} FROM memory_documents "
                "WHERE source_path = %s OR name = %s ORDER BY doc_kind LIMIT 1",
                (name_or_path, name_or_path),
            )
            row = cur.fetchone()
    return _row_to_document(row) if row else None


def get_router(*, dsn: Optional[str] = None) -> Optional[Document]:
    """The always-loaded router document, or ``None``."""
    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_DOC_COLUMNS} FROM memory_documents "
                "WHERE doc_kind = 'router' LIMIT 1"
            )
            row = cur.fetchone()
    return _row_to_document(row) if row else None


def list_documents(*, dsn: Optional[str] = None) -> List[Document]:
    """Every stored document, router first then notes by name."""
    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_DOC_COLUMNS} FROM memory_documents "
                "ORDER BY (doc_kind = 'router') DESC, name"
            )
            rows = cur.fetchall()
    return [_row_to_document(r) for r in rows]


def delete_document(name_or_path: str, *, dsn: Optional[str] = None) -> bool:
    """Delete a document (chunks cascade). Returns whether a row went away."""
    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM memory_documents WHERE source_path = %s OR name = %s",
                (name_or_path, name_or_path),
            )
            deleted = cur.rowcount > 0
        conn.commit()
    return deleted


def count_documents(*, dsn: Optional[str] = None) -> int:
    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memory_documents")
            return int(cur.fetchone()[0])


def count_chunks(*, dsn: Optional[str] = None) -> int:
    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memory_chunks")
            return int(cur.fetchone()[0])


def router_body_bytes(*, dsn: Optional[str] = None) -> Optional[int]:
    """Body size of the router document — the number ``budget_bytes`` caps."""
    router = get_router(dsn=dsn)
    return router.body_bytes if router else None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_RRF_K = 60


def semantic_search(
    query: str,
    k: int = 5,
    *,
    query_vec: Optional[Vector] = None,
    dsn: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Nearest neighbours by cosine similarity. Returns ``[]`` if no vectors."""
    if query_vec is None:
        from . import embedder

        query_vec = embedder.embed_query(query)
    if query_vec is None:
        return []

    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.name, d.doc_kind, c.heading, c.content,
                       1 - (c.embedding <=> %s) AS score
                FROM memory_chunks c
                JOIN memory_documents d ON d.id = c.document_id
                WHERE c.embedding IS NOT NULL
                ORDER BY c.embedding <=> %s
                LIMIT %s
                """,
                (as_vector(query_vec), as_vector(query_vec), int(k)),
            )
            rows = cur.fetchall()
    return [
        {"name": r[0], "doc_kind": r[1], "heading": r[2], "content": r[3],
         "score": float(r[4]), "match": "semantic"}
        for r in rows
    ]


def keyword_search(query: str, k: int = 5, *, dsn: Optional[str] = None) -> List[Dict[str, Any]]:
    """Full-text search over the generated ``tsv`` column."""
    if not (query or "").strip():
        return []
    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.name, d.doc_kind, c.heading, c.content,
                       ts_rank_cd(c.tsv, plainto_tsquery('english', %s)) AS score
                FROM memory_chunks c
                JOIN memory_documents d ON d.id = c.document_id
                WHERE c.tsv @@ plainto_tsquery('english', %s)
                ORDER BY score DESC
                LIMIT %s
                """,
                (query, query, int(k)),
            )
            rows = cur.fetchall()
    return [
        {"name": r[0], "doc_kind": r[1], "heading": r[2], "content": r[3],
         "score": float(r[4]), "match": "keyword"}
        for r in rows
    ]


def search(
    query: str,
    k: int = 5,
    *,
    query_vec: Optional[Vector] = None,
    dsn: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Hybrid retrieval: vector + keyword fused with reciprocal rank fusion.

    Degrades instead of failing. If no query vector can be produced (embedding
    backend down) it returns keyword results, which is exactly the behaviour the
    design asks for.
    """
    if query_vec is None:
        from . import embedder

        query_vec = embedder.embed_query(query)

    if query_vec is None:
        return keyword_search(query, k=k, dsn=dsn)

    with connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH vec AS (
                    SELECT c.id,
                           row_number() OVER (ORDER BY c.embedding <=> %(vec)s) AS rnk
                    FROM memory_chunks c
                    WHERE c.embedding IS NOT NULL
                    ORDER BY c.embedding <=> %(vec)s
                    LIMIT %(k)s
                ), kw AS (
                    SELECT c.id,
                           row_number() OVER (
                               ORDER BY ts_rank_cd(c.tsv, plainto_tsquery('english', %(q)s)) DESC
                           ) AS rnk
                    FROM memory_chunks c
                    WHERE c.tsv @@ plainto_tsquery('english', %(q)s)
                    LIMIT %(k)s
                )
                SELECT d.name, d.doc_kind, c.heading, c.content,
                       COALESCE(1.0 / (%(rrf)s + vec.rnk), 0)
                     + COALESCE(1.0 / (%(rrf)s + kw.rnk), 0) AS score,
                       (vec.rnk IS NOT NULL) AS from_vec,
                       (kw.rnk IS NOT NULL) AS from_kw
                FROM vec FULL OUTER JOIN kw ON vec.id = kw.id
                JOIN memory_chunks c ON c.id = COALESCE(vec.id, kw.id)
                JOIN memory_documents d ON d.id = c.document_id
                ORDER BY score DESC
                LIMIT %(k)s
                """,
                {"vec": as_vector(query_vec), "q": query, "k": int(k), "rrf": _RRF_K},
            )
            rows = cur.fetchall()

    results: List[Dict[str, Any]] = []
    for r in rows:
        from_vec, from_kw = bool(r[5]), bool(r[6])
        match = "hybrid" if (from_vec and from_kw) else ("semantic" if from_vec else "keyword")
        results.append(
            {"name": r[0], "doc_kind": r[1], "heading": r[2], "content": r[3],
             "score": float(r[4]), "match": match}
        )
    return results
