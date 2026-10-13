"""The panel's ``/memory`` page rendered against the PostgreSQL memory store.

``tests/test_panel_memory.py`` drives the same page through the Markdown
compatibility backend, because ``tests/conftest.py`` pins that backend for the
pre-existing suite.  Nothing there covers the store the panel actually edits, so
these tests select ``CLAIRE_MEMORY_BACKEND=postgres`` themselves and read the
page the owner sees when memory lives in Postgres.

They assert on *what the page shows*, not on a particular constant: a page that
tells the owner their memory is a Markdown vault, a filename under the editor or
an over-budget warning naming ``CLAIRE.md`` is failing regardless of where the
string is defined.

These tests run against the ``claire_test`` database.  They skip cleanly when
Postgres is unreachable so the suite stays green on a machine without the
container, and they must never touch the production ``claire`` database.
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from pathlib import Path

import pytest

from claire.memory import pg
from claire.memory.pg import MemoryStoreError
from claire.panel import server as panel
from claire.panel.server import ROUTE_INDEX, ROUTE_MEMORY, RealPanelState

# These tests need a live PostgreSQL with the pgvector extension; the fixtures
# skip themselves when it is unreachable, and refuse to run destructively
# against the production DSN.
pytestmark = pytest.mark.integration

ROUTER_BODY = (
    "---\n"
    "title: Claire's memory router\n"
    "role: router\n"
    "always_loaded: true\n"
    "budget_bytes: 6000\n"
    "---\n"
    "\n"
    "# About you\n"
    "\n"
    "- name: the owner of this machine.\n"
    "\n"
    "## Pointers\n"
    "\n"
    "- dentist: the dentist appointment is on Tuesday.\n"
)


def _seed_router(body: str = ROUTER_BODY, *, budget: int = 6000) -> None:
    pg.upsert_document(
        source_path="CLAIRE.md",
        doc_kind="router",
        name="CLAIRE",
        title="Claire's memory router",
        body=body,
        frontmatter={
            "title": "Claire's memory router",
            "role": "router",
            "always_loaded": True,
            "budget_bytes": budget,
        },
        always_loaded=True,
        budget_bytes=budget,
    )


def _seed_note(name: str, body: str, *, title: str = "") -> None:
    pg.upsert_document(
        source_path=f"notes/{name}.md",
        doc_kind="note",
        name=name,
        title=title or name,
        body=body,
        frontmatter={"title": title or name},
    )


def _seed_memory() -> None:
    """The router, a note it names, and one it never mentions."""
    _seed_router()
    _seed_note(
        "dentist",
        "Dr. Ruiz holds the appointment on Tuesday at 16:00.\n",
        title="Dentist",
    )
    _seed_note(
        "loose-ends",
        "Details the router keeps no pointer to.\n",
        title="Loose ends",
    )


def _write_database(path: Path) -> None:
    """A minimal stand-in for the conversation database, so nothing reads the real one."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE conversation_summaries (
            id INTEGER PRIMARY KEY,
            date_utc TEXT,
            ts_utc TEXT,
            summary TEXT,
            topics TEXT,
            source_app TEXT
        );
        CREATE VIRTUAL TABLE summaries_fts USING fts5(summary, topics);
        """
    )
    connection.commit()
    connection.close()


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
def store(dsn: str, monkeypatch: pytest.MonkeyPatch):
    """An empty ``claire_test`` store that the panel's own readers resolve to."""
    pg.close_pool()
    pg.reset_schema(dsn)
    pg.bootstrap_schema(dsn)
    # The panel's Postgres readers call the store with ``dsn=None``; pointing the
    # environment DSN at ``claire_test`` is what keeps them off the live database.
    monkeypatch.setenv("CLAIRE_PG_DSN", dsn)
    monkeypatch.setenv("CLAIRE_MEMORY_BACKEND", "postgres")
    import claire.system_prompt as system_prompt

    monkeypatch.setattr(system_prompt, "_memory_backend_cached", None)
    pg.close_pool()
    yield dsn
    pg.close_pool()
    pg.reset_schema(dsn)
    pg.close_pool()


@pytest.fixture()
def state(tmp_path: Path, store: str) -> RealPanelState:
    database = tmp_path / "claire.db"
    _write_database(database)
    return RealPanelState(
        vault_dir=tmp_path / "vault",
        diary_path=database,
        config_path=tmp_path / "config.json",
        action_log_path=tmp_path / "actions.jsonl",
        now=lambda: datetime.date(2026, 10, 6),
    )


@pytest.fixture()
def memory(state: RealPanelState) -> RealPanelState:
    _seed_memory()
    return state


def get(path: str, state: RealPanelState) -> panel.PanelResponse:
    return panel.dispatch(state, panel.PanelRequest(method="GET", path=path))


def page(path: str, state: RealPanelState) -> str:
    """A page's visible markup: the inlined stylesheet is not shown to the owner."""
    return re.sub(r"<style.*?</style>", "", get(path, state).text, flags=re.S)


# ---------------------------------------------------------------------------
# The copy describes the store that is actually in use
# ---------------------------------------------------------------------------

def test_store_pages_never_call_the_memory_a_markdown_vault(
    memory: RealPanelState,
) -> None:
    for route in (ROUTE_INDEX, ROUTE_MEMORY):
        body = get(route, memory).text
        low = body.lower()
        assert "markdown" not in low, route
        assert "obsidian" not in low, route
        assert "vault" not in low, route
        assert "CLAIRE.md" not in body, route


def test_store_memory_page_names_the_store_and_its_rows(
    memory: RealPanelState,
) -> None:
    body = get(ROUTE_MEMORY, memory).text
    assert "PostgreSQL" in body
    assert "router" in body
    assert "dentist" in body
    assert "loose-ends" in body


def test_store_note_the_router_never_mentions_is_marked_rather_than_hidden(
    memory: RealPanelState,
) -> None:
    body = get(ROUTE_MEMORY, memory).text
    rows = re.findall(r"<tr>.*?</tr>", body, re.S)
    loose = next(row for row in rows if "loose-ends" in row)
    dentist = next(row for row in rows if ">dentist<" in row)
    assert 'class="warn"' in loose
    assert 'class="warn"' not in dentist
    assert "router never mentions this note" in loose


def test_store_open_note_shows_no_filename_and_no_vault(
    memory: RealPanelState,
) -> None:
    body = page(f"{ROUTE_MEMORY}?note=dentist", memory)
    low = body.lower()
    assert "obsidian" not in low
    assert "vault" not in low
    assert ".md" not in body
    assert "removed from the store" in body
    assert "Dr. Ruiz holds the appointment" in body


def test_store_new_note_form_does_not_claim_a_markdown_file(
    memory: RealPanelState,
) -> None:
    body = page(ROUTE_MEMORY, memory)
    low = body.lower()
    assert "obsidian" not in low
    assert "vault" not in low
    assert ".md" not in body
    assert "the filename" not in body


# ---------------------------------------------------------------------------
# The over-budget warning comes from the store router, not a file
# ---------------------------------------------------------------------------

def test_store_router_over_budget_warning_names_the_store(
    state: RealPanelState,
) -> None:
    head = (
        "---\n"
        "title: Claire's memory router\n"
        "role: router\n"
        "always_loaded: true\n"
        "budget_bytes: 6000\n"
        "---\n\n"
    )
    body = "".join(
        f"- fact number {n}: some standing detail worth a line.\n" for n in range(400)
    )
    _seed_router(head + body)

    status = state.router_status()
    assert status.over_budget is True
    assert status.warning, "the store router must supply its own over-budget framing"
    assert "CLAIRE.md" not in status.warning

    body = get(ROUTE_INDEX, state).text
    assert "OVER BUDGET" in body
    assert f"{status.size_bytes} bytes of {status.budget_bytes} bytes" in body
    assert "Real size:" in body
    assert "Dropped:" in body
    assert "the end of the file" not in body
    assert "CLAIRE.md" not in body
