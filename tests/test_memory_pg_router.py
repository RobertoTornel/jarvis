"""Integration tests: the Postgres router must be indistinguishable from the file router.

The cutover is only safe if the prompt the model sees does not change. These
tests assert exactly that, byte for byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claire.memory import pg
from claire.memory import migrate_md_to_pg as mig
from claire.memory.pg import MemoryStoreError
from claire.system_prompt import (
    MEMORY_SECTION_HEADING,
    MEMORY_UNAVAILABLE_MARKER,
    build_memory_section,
    build_system_prompt,
)

# These tests need a live PostgreSQL with the pgvector extension; the fixtures
# skip themselves when it is unreachable, and refuse to run destructively
# against the production DSN.
pytestmark = pytest.mark.integration

ROUTER_MD = (
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
    "- name: the owner of this machine; use it when you speak to them.\n"
    "- timezone: local time on this machine.\n"
    "\n"
    "## Notes\n"
    "\n"
    "- Anything longer belongs in notes/, with only a pointer here.\n"
)


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
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "CLAIRE.md").write_text(ROUTER_MD, encoding="utf-8")
    return root


@pytest.fixture()
def loaded(dsn: str, vault: Path):
    pg.reset_schema(dsn)
    pg.bootstrap_schema(dsn)
    mig.import_vault(vault, dsn=dsn, verbose=False)
    yield dsn, vault
    pg.reset_schema(dsn)
    pg.close_pool()


# ---------------------------------------------------------------------------

def test_postgres_and_file_routers_render_the_same_body(loaded):
    """The cutover must not change the memory *content* the model receives.

    The header legitimately differs: it tells the model where memory lives, and
    after the cutover there is no ``CLAIRE.md`` to point at. The body, the
    heading and the budget behaviour must be identical.
    """
    dsn, vault = loaded

    from claire.memory.pg_router import load_router_from_postgres
    from claire.memory.router import load_router

    pg_router = load_router_from_postgres(dsn)
    file_router = load_router(vault / "CLAIRE.md")

    assert pg_router.body == file_router.body, "the stored body differs from the file body"
    assert pg_router.size_bytes == file_router.size_bytes

    from_pg = build_memory_section(pg_router)
    from_file = build_memory_section(file_router)

    assert from_pg.startswith(MEMORY_SECTION_HEADING)
    # Strip each backend's own header line block and compare the memory content.
    pg_tail = from_pg.split("\n\n", 1)[1]
    file_tail = from_file.split("\n\n", 1)[1]
    assert pg_tail.split("\n\n", 1)[1] == file_tail.split("\n\n", 1)[1]


def test_postgres_header_does_not_point_at_a_retired_file(loaded):
    """The prompt must not tell the model to edit CLAIRE.md, which no longer exists."""
    dsn, _vault = loaded
    from claire.memory.pg_router import load_router_from_postgres

    rendered = load_router_from_postgres(dsn).render_for_prompt()
    assert "CLAIRE.md" not in rendered, "the prompt still points at the retired vault file"
    assert "Memory page" in rendered
    assert "no restart" in rendered


def test_store_router_over_budget_warning_names_no_file_or_notes_directory(loaded):
    """The notice the model reads must describe the router, not a path.

    Over budget, the store path serves the same warning as the vault path: the
    real size, the budget and the dropped bytes, with no ``CLAIRE.md``, no
    ``notes/`` directory and no vault.
    """
    dsn, _vault = loaded
    from claire.memory.pg_router import load_router_from_postgres

    stored = pg.get_router(dsn=dsn)
    assert stored is not None
    budget = 300
    body = "".join(f"fact-{i:02d}: " + "y" * 40 + "\n" for i in range(30))
    pg.upsert_document(
        source_path=stored.source_path,
        doc_kind="router",
        name=stored.name,
        title=stored.title,
        body=body,
        frontmatter={"budget_bytes": str(budget), "role": "router"},
        always_loaded=True,
        budget_bytes=budget,
        dsn=dsn,
    )

    router = load_router_from_postgres(dsn)
    assert router.over_budget is True
    assert router.dropped_bytes > 0

    # The warning is the block prepended to the block the model reads.
    warning = router.render_for_prompt().split("\n\n", 1)[0]

    assert warning.startswith("!" * 72) and warning.endswith("!" * 72)
    assert "OVER BUDGET" in warning
    assert f"Real size: {router.size_bytes} bytes" in warning
    assert f"Budget: {router.budget_bytes} bytes" in warning
    assert f"Dropped: {router.dropped_bytes} bytes" in warning
    for untrue in ("claire.md", "notes/", "vault", "file"):
        assert untrue not in warning.lower(), f"store warning names {untrue!r}: {warning}"

    # And it really is inside the prompt section, not only in a standalone render.
    section = build_memory_section(router)
    assert section.startswith(MEMORY_SECTION_HEADING)
    assert warning in section


def test_postgres_router_preserves_budget_semantics(loaded):
    """budget_bytes/size_bytes/over_budget must come from the stored frontmatter."""
    dsn, _vault = loaded

    from claire.memory.pg_router import load_router_from_postgres
    from claire.memory.router import load_router

    pg_router = load_router_from_postgres(dsn)
    file_router = load_router(_vault / "CLAIRE.md")

    assert pg_router.budget_bytes == file_router.budget_bytes == 6000
    assert pg_router.size_bytes == file_router.size_bytes
    assert pg_router.over_budget == file_router.over_budget is False


def test_router_reload_picks_up_a_panel_edit(loaded):
    """An edit written through the store is visible on the next reload."""
    dsn, _vault = loaded
    from claire.memory.pg_router import load_router_from_postgres

    router = load_router_from_postgres(dsn)
    before = router.size_bytes

    doc = pg.get_router(dsn=dsn)
    pg.upsert_document(
        source_path=doc.source_path, doc_kind="router", name=doc.name,
        body=doc.body + "\n- a brand new fact.\n", frontmatter=doc.frontmatter,
        frontmatter_raw=doc.frontmatter_raw, always_loaded=True,
        budget_bytes=doc.budget_bytes, dsn=dsn,
    )

    router.reload()
    assert router.size_bytes > before
    assert "a brand new fact" in router.body


def test_router_save_round_trips_through_the_store(loaded):
    dsn, _vault = loaded
    from claire.memory.pg_router import load_router_from_postgres

    router = load_router_from_postgres(dsn)
    router.body = "# About you\n\n- name: someone else.\n"
    router.save()

    assert "someone else" in load_router_from_postgres(dsn).body


def test_missing_router_is_reported_not_raised(dsn: str, monkeypatch):
    """A forced read with no stored router must degrade to the named marker."""
    pg.reset_schema(dsn)
    pg.bootstrap_schema(dsn)

    import claire.system_prompt as sp

    monkeypatch.setenv("CLAIRE_PG_DSN", dsn)
    monkeypatch.setenv("CLAIRE_MEMORY_BACKEND", "postgres")

    section = sp._resolve_memory_section(None, None, True)
    assert section is not None
    assert MEMORY_UNAVAILABLE_MARKER in section, section
    pg.reset_schema(dsn)
    pg.close_pool()


def test_automatic_mode_stays_silent_without_a_router(dsn: str, monkeypatch):
    """load_default_router=None must not invent a warning when nothing is stored."""
    pg.reset_schema(dsn)
    pg.bootstrap_schema(dsn)

    import claire.system_prompt as sp

    monkeypatch.setenv("CLAIRE_PG_DSN", dsn)
    monkeypatch.setenv("CLAIRE_MEMORY_BACKEND", "postgres")

    assert sp._resolve_memory_section(None, None, None) is None
    pg.reset_schema(dsn)
    pg.close_pool()


def test_unreachable_database_is_not_fatal(monkeypatch):
    """A dead database must produce a marker, never an exception in the prompt."""
    import claire.system_prompt as sp

    monkeypatch.setenv("CLAIRE_PG_DSN", "postgresql://nobody@127.0.0.1:1/nothing")
    monkeypatch.setenv("CLAIRE_MEMORY_BACKEND", "postgres")
    pg.close_pool()

    section = sp._resolve_memory_section(None, None, True)
    assert section is not None and MEMORY_UNAVAILABLE_MARKER in section
    # And the whole prompt still builds.
    prompt = build_system_prompt("Claire", load_default_router=True)
    assert isinstance(prompt, str) and prompt
    pg.close_pool()
