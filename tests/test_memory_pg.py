"""Tests for the PostgreSQL + pgvector memory backend.

These tests run against the ``claire_test`` database. They skip cleanly when
Postgres is unreachable so the suite stays green on a machine without the
container. They must never touch the production ``claire`` database.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claire.memory import pg
from claire.memory import migrate_md_to_pg as mig
from claire.memory.pg import Chunk, MemoryStoreError


# These tests need a live PostgreSQL with the pgvector extension; the
# fixtures skip themselves when it is unreachable, and refuse to run
# destructively against the production DSN.
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dsn() -> str:
    try:
        target = pg.test_dsn()
    except MemoryStoreError as e:
        pytest.skip(f"no test DSN configured: {e}")
    ok, detail = pg.health(target)
    if not ok:
        pytest.skip(f"Postgres test database unreachable: {detail}")
    if target == pg.resolve_dsn():
        pytest.skip("refusing to run destructive tests against the production DSN")
    return target


@pytest.fixture()
def store(dsn: str):
    pg.reset_schema(dsn)
    pg.bootstrap_schema(dsn)
    yield dsn
    pg.reset_schema(dsn)
    pg.close_pool()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """A self-contained vault, so tests never depend on the owner's live files."""
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "CLAIRE.md").write_text(
        "---\n"
        "title: Claire's memory router\n"
        "role: router\n"
        "always_loaded: true\n"
        "budget_bytes: 6000\n"
        "updated: 2026-10-06\n"
        "---\n"
        "\n"
        "# About you\n"
        "\n"
        "- name: the owner of this machine.\n"
        "\n"
        "## Voice\n"
        "\n"
        "She speaks with piper, briefly.\n",
        encoding="utf-8",
    )
    (root / "notes" / "dentist.md").write_text(
        "---\n"
        "title: Dentist\n"
        "---\n"
        "\n"
        "# Dentist\n"
        "\n"
        "The dentist appointment is on Tuesday.\n"
        "\n"
        "## Notes\n"
        "\n"
        "Bring the insurance card.\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_bootstrap_is_idempotent(store: str):
    assert pg.bootstrap_schema(store) is False  # already applied by the fixture
    with pg.connection(store) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT amname FROM pg_am WHERE amname IN ('hnsw','ivfflat')")
            assert "hnsw" in {r[0] for r in cur.fetchall()}


def test_hnsw_index_exists_with_cosine_ops(store: str):
    with pg.connection(store) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
                ("memory_chunks_embedding_hnsw_idx",),
            )
            row = cur.fetchone()
    assert row, "the HNSW index was not created"
    assert "hnsw" in row[0] and "vector_cosine_ops" in row[0]


def test_importing_the_module_does_not_connect():
    """Import-time must be side-effect free: a bogus DSN must not break import.

    Run in a subprocess so the module's cached pool is not disturbed for the
    other tests in this session.
    """
    import subprocess
    import sys

    code = (
        "import claire.memory.pg as pg;"
        "print('imported', pg.SCHEMA_VERSION);"
        "ok, detail = pg.health();"
        "print('health', ok)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            "CLAIRE_PG_DSN": "postgresql://nobody@127.0.0.1:1/nothing",
        },
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "imported" in proc.stdout
    assert "health False" in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def test_upsert_get_delete_round_trip(store: str):
    doc_id, inserted = pg.upsert_document(
        source_path="notes/a.md", doc_kind="note", name="a", title="A",
        body="Alpha body.", frontmatter={"title": "A"}, chunks=[],
        dsn=store,
    )
    assert inserted is True

    got = pg.get_document("a", dsn=store)
    assert got is not None and got.body == "Alpha body." and got.id == doc_id

    _id2, inserted2 = pg.upsert_document(
        source_path="notes/a.md", doc_kind="note", name="a", title="A",
        body="Alpha body.", frontmatter={"title": "A"}, chunks=[], dsn=store,
    )
    assert inserted2 is False, "re-upserting identical content must not insert"
    assert pg.count_documents(dsn=store) == 1

    assert pg.delete_document("a", dsn=store) is True
    assert pg.get_document("a", dsn=store) is None


def test_delete_cascades_to_chunks(store: str):
    pg.upsert_document(
        source_path="notes/b.md", doc_kind="note", name="b", body="Body.",
        frontmatter={}, chunks=[Chunk(index=0, content="Body.", char_end=5)], dsn=store,
    )
    assert pg.count_chunks(dsn=store) == 1
    pg.delete_document("b", dsn=store)
    assert pg.count_chunks(dsn=store) == 0


def test_unknown_doc_kind_is_rejected(store: str):
    with pytest.raises(MemoryStoreError):
        pg.upsert_document(
            source_path="x.md", doc_kind="nonsense", name="x", body="", dsn=store
        )


def test_router_body_bytes_reports_the_budgeted_number(store: str):
    body = "# Router\n\nSome router prose.\n"
    pg.upsert_document(
        source_path="CLAIRE.md", doc_kind="router", name="CLAIRE", body=body,
        frontmatter={"budget_bytes": 6000}, always_loaded=True, budget_bytes=6000,
        chunks=[], dsn=store,
    )
    assert pg.router_body_bytes(dsn=store) == len(body.encode("utf-8"))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_semantic_search_returns_the_closest_chunk(store: str):
    from claire.memory import embedder

    texts = {
        0: "The panel uses dark gray surfaces with a pink accent.",
        1: "Building the Android companion app with Gradle.",
        2: "Reducing end-to-end voice turn latency in the assistant.",
    }
    query_vec = embedder.embed_query("how do I make the interface calmer?")
    if query_vec is None:
        pytest.skip("embedding backend unavailable")

    chunks = []
    for index, text in texts.items():
        vec = embedder.embed_document(text)
        if vec is None:
            pytest.skip("embedding backend unavailable")
        chunks.append(Chunk(index=index, content=text, embedding=vec, char_end=len(text)))

    pg.upsert_document(
        source_path="notes/ui.md", doc_kind="note", name="ui", body="\n".join(texts.values()),
        frontmatter={}, chunks=chunks, dsn=store,
    )

    hits = pg.semantic_search("how do I make the interface calmer?", k=3,
                              query_vec=query_vec, dsn=store)
    assert hits, "semantic search returned nothing"
    assert "dark gray" in hits[0]["content"], f"wrong top hit: {hits[0]}"
    assert hits[0]["score"] > hits[-1]["score"], "results are not ordered by similarity"


def test_search_degrades_to_keyword_without_a_vector(store: str, monkeypatch):
    pg.upsert_document(
        source_path="notes/k.md", doc_kind="note", name="k",
        body="The dentist appointment is on Tuesday.",
        frontmatter={}, chunks=[Chunk(index=0, content="The dentist appointment is on Tuesday.",
                                      char_end=37)], dsn=store,
    )
    keyword = pg.keyword_search("dentist", k=5, dsn=store)
    assert [h["name"] for h in keyword] == ["k"]

    # Simulate the embedding backend being down: search must degrade to the
    # keyword half rather than raising.
    from claire.memory import embedder

    monkeypatch.setattr(embedder, "embed_query", lambda *a, **k: None)
    hits = pg.search("dentist", k=5, query_vec=None, dsn=store)
    assert [h["name"] for h in hits] == ["k"]
    assert hits[0]["match"] == "keyword"


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def test_import_is_idempotent(store: str, vault: Path):
    first = mig.import_vault(vault, dsn=store, verbose=False)
    assert first.created == 2, first.render()
    docs_after_first = pg.count_documents(dsn=store)
    chunks_after_first = pg.count_chunks(dsn=store)

    second = mig.import_vault(vault, dsn=store, verbose=False)
    assert second.created == 0
    assert second.unchanged == 2, second.render()
    assert pg.count_documents(dsn=store) == docs_after_first
    assert pg.count_chunks(dsn=store) == chunks_after_first


def test_dry_run_writes_nothing(store: str, vault: Path):
    report = mig.import_vault(vault, dsn=store, dry_run=True, verbose=False)
    assert report.created + report.updated == 2
    assert pg.count_documents(dsn=store) == 0
    assert pg.count_chunks(dsn=store) == 0


def test_frontmatter_only_edit_does_not_reembed(store: str, vault: Path):
    mig.import_vault(vault, dsn=store, verbose=False)
    with pg.connection(store) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT embedded_at FROM memory_chunks ORDER BY chunk_index LIMIT 1")
            before = cur.fetchone()[0]

    path = vault / "CLAIRE.md"
    path.write_text(path.read_text().replace("updated: 2026-10-06", "updated: 2026-10-07"))

    report = mig.import_vault(vault, dsn=store, verbose=False)
    assert report.updated == 1 and report.created == 0

    with pg.connection(store) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT embedded_at FROM memory_chunks ORDER BY chunk_index LIMIT 1")
            after = cur.fetchone()[0]
    assert before == after, "a frontmatter-only edit re-embedded unchanged prose"


def test_export_round_trips_byte_identically(store: str, vault: Path, tmp_path: Path):
    mig.import_vault(vault, dsn=store, verbose=False)
    out = tmp_path / "restored"
    mig.export_vault(out, dsn=store)

    for source in sorted(vault.rglob("*.md")):
        restored = out / source.relative_to(vault)
        assert restored.exists(), f"{source.name} was not exported"
        assert restored.read_text(encoding="utf-8") == source.read_text(encoding="utf-8"), (
            f"{source.name} did not round-trip byte-identically"
        )


def test_chunking_splits_at_headings(vault: Path):
    _meta, body = mig.parse_frontmatter((vault / "CLAIRE.md").read_text())
    chunks = mig.chunk_body(body)
    assert len(chunks) >= 2
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert any(c.heading == "Voice" for c in chunks)
    joined = "".join(c.content for c in chunks)
    assert "piper" in joined


def test_chunking_handles_empty_body():
    assert mig.chunk_body("") == []
    assert mig.chunk_body("   \n  ") == []
