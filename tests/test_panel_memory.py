"""Unit tests for the panel's memories browser (routes ``/memory`` and friends).

The browser exists to make Claire's memory *inspectable* and *editable* without
a second implementation of anything: listing, reading, searching and writing all
go through :mod:`claire.memory.notes` and :mod:`claire.memory.obsidian`, which
is what these tests pin down.  If the panel ever grew its own note format, the
round-trip tests here would fail on the next read.

Nothing touches the real vault or the real database: every state is built with
an injected ``vault_dir`` and ``diary_path`` inside ``tmp_path``.

The gate is ``pytest tests/ -m unit`` and it SILENTLY DESELECTS a file with no
unit marker — hence ``pytestmark = pytest.mark.unit`` below.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claire.memory import notes as notes_module
from claire.memory import obsidian as obsidian_module
from claire.panel import server as panel
from claire.panel.server import (
    DEFAULT_PORT,
    ROUTE_MEMORY,
    ROUTE_MEMORY_DELETE,
    ROUTE_MEMORY_SAVE,
    RealPanelState,
)

pytestmark = pytest.mark.unit

FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Host": f"127.0.0.1:{DEFAULT_PORT}",
    "Origin": f"http://127.0.0.1:{DEFAULT_PORT}",
}


def _write_vault(vault: Path) -> None:
    (vault / "notes").mkdir(parents=True)
    (vault / "CLAIRE.md").write_text(
        "---\n"
        "title: Claire's memory router\n"
        "role: router\n"
        "always_loaded: true\n"
        "---\n\n"
        "# Pointers\n\n"
        "- dentist: [[dentist]]\n"
        "- car: notes/car-service.md\n",
        encoding="utf-8",
    )
    (vault / "notes" / "dentist.md").write_text(
        "---\n"
        "title: Dentist\n"
        "created: 2026-01-01\n"
        "---\n\n"
        "Dr. Ruiz, Wednesday 16:00. See [[car-service|the car]] and "
        "[[no-such-note]].\n",
        encoding="utf-8",
    )
    (vault / "notes" / "car-service.md").write_text(
        "---\ntitle: Car\n---\n\nGarage on Main St, 45000 km.\n",
        encoding="utf-8",
    )
    # Deliberately not mentioned by CLAIRE.md: the browser must call it out.
    (vault / "notes" / "orphan.md").write_text(
        "---\ntitle: Orphan\n---\n\nNobody points at me.\n",
        encoding="utf-8",
    )


def _write_database(path: Path) -> None:
    """A minimal stand-in for the memory database: summaries plus its FTS index."""
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
        INSERT INTO conversation_summaries
            (date_utc, ts_utc, summary, topics, source_app)
        VALUES
            ('2026-02-01', '2026-02-01T08:00:00Z',
             'Owner asked about the dentist appointment.', 'dentist', 'panel');
        INSERT INTO summaries_fts (rowid, summary, topics)
        VALUES (1, 'Owner asked about the dentist appointment.', 'dentist');
        """
    )
    connection.commit()
    connection.close()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    directory = tmp_path / "vault"
    _write_vault(directory)
    return directory


@pytest.fixture()
def state(tmp_path: Path, vault: Path) -> RealPanelState:
    database = tmp_path / "claire.db"
    _write_database(database)
    return RealPanelState(
        vault_dir=vault,
        diary_path=database,
        config_path=tmp_path / "config.json",
        action_log_path=tmp_path / "actions.jsonl",
    )


def get(path: str, state: RealPanelState) -> panel.PanelResponse:
    return panel.dispatch(state, panel.PanelRequest(method="GET", path=path))


def post(
    path: str,
    body: bytes,
    state: RealPanelState,
    headers: dict[str, str] | None = None,
) -> panel.PanelResponse:
    return panel.dispatch(
        state,
        panel.PanelRequest(
            method="POST",
            path=path,
            body=body,
            headers=headers if headers is not None else FORM_HEADERS,
        ),
    )


# ---------------------------------------------------------------------------
# Reading: the router, every note, and one note opened
# ---------------------------------------------------------------------------


def test_memory_page_lists_router_and_every_note(state: RealPanelState) -> None:
    response = get(ROUTE_MEMORY, state)
    assert response.status == 200
    body = response.text
    assert "Router (CLAIRE.md)" in body
    assert "memory router" in body
    for name in ("dentist", "car-service", "orphan"):
        assert name in body, name
    # The vault path is shown, not guessed at.
    assert state.vault_path().name == "vault"
    assert str(state.vault_path()) in body


def test_orphaned_note_is_marked_rather_than_hidden(state: RealPanelState) -> None:
    body = get(ROUTE_MEMORY, state).text
    assert "orphan" in body
    assert "never mentions this note" in body


def test_open_note_shows_frontmatter_body_and_resolvable_links(
    state: RealPanelState,
) -> None:
    response = get(f"{ROUTE_MEMORY}?note=dentist", state)
    assert response.status == 200
    body = response.text
    assert "Dr. Ruiz" in body
    assert "<th>Frontmatter</th>" in body
    assert "created" in body and "2026-01-01" in body
    # A link that resolves is a link; one that does not is called out, not
    # silently dropped and not silently created.
    assert f'[[' in body and "car-service" in body
    assert "no-such-note" in body
    assert "no note with this name" in body
    assert not (state.vault_path() / "notes" / "no-such-note.md").exists()


def test_open_note_refuses_a_traversal_name(state: RealPanelState) -> None:
    response = get(f"{ROUTE_MEMORY}?note=../etc/passwd", state)
    assert response.status == 200  # a rendered refusal, not a crash
    assert "InvalidNoteNameError" in response.text
    assert "../etc/passwd" in response.text


def test_missing_note_is_named_not_blank(state: RealPanelState) -> None:
    body = get(f"{ROUTE_MEMORY}?note=ghost", state).text
    assert "NoteNotFoundError" in body
    assert "ghost" in body


def test_note_body_is_escaped_not_executed(vault: Path, state: RealPanelState) -> None:
    (vault / "notes" / "html.md").write_text(
        '---\ntitle: "<b>bold</b>"\n---\n\n<script>alert(1)</script>\n',
        encoding="utf-8",
    )
    body = get(f"{ROUTE_MEMORY}?note=html", state).text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<b>bold</b>" not in body


def test_malformed_frontmatter_is_reported(state: RealPanelState) -> None:
    (state.vault_path() / "notes" / "broken.md").write_text(
        "---\ntitle: Never closed\n\nbody\n", encoding="utf-8"
    )
    body = get(f"{ROUTE_MEMORY}?note=broken", state).text
    assert "never closed by a matching" in body
    assert "Properties table" in body


# ---------------------------------------------------------------------------
# Searching: one query, both stores
# ---------------------------------------------------------------------------


def test_search_finds_a_note_and_a_persisted_summary(state: RealPanelState) -> None:
    body = get(f"{ROUTE_MEMORY}?q=dentist", state).text
    assert "note hit" in body
    assert "Dr. Ruiz" in body  # the matching note line
    assert "persisted summary hit" in body
    assert "Owner asked about the dentist appointment." in body


def test_search_with_no_hits_says_so_in_both_stores(state: RealPanelState) -> None:
    body = get(f"{ROUTE_MEMORY}?q=zzzznotathing", state).text
    assert "No note matched" in body
    assert "No persisted summary matched" in body


def test_search_reads_the_same_note_search_claire_uses(
    state: RealPanelState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel calls ``notes.search_notes``; it does not search on its own."""
    calls: list[tuple[str, object]] = []
    original = notes_module.search_notes

    def spy(query, notes_dir=None, limit=10, vault_dir=None):
        calls.append((query, vault_dir))
        return original(query, notes_dir=notes_dir, limit=limit, vault_dir=vault_dir)

    monkeypatch.setattr(notes_module, "search_notes", spy)
    get(f"{ROUTE_MEMORY}?q=garage", state)
    assert calls and calls[0][0] == "garage"


def test_search_without_a_notes_directory_still_reports_the_diary(
    tmp_path: Path,
) -> None:
    """A vault with no ``notes/`` is a named miss, not a blank page."""
    vault = tmp_path / "bare-vault"
    vault.mkdir()
    (vault / "CLAIRE.md").write_text("# Router\n", encoding="utf-8")
    database = tmp_path / "claire.db"
    _write_database(database)
    state = RealPanelState(vault_dir=vault, diary_path=database)
    body = get(f"{ROUTE_MEMORY}?q=dentist", state).text
    assert "NotesDirectoryError" in body
    assert "persisted summary hit" in body


# ---------------------------------------------------------------------------
# Writing: create, edit, delete — all through claire.memory.notes
# ---------------------------------------------------------------------------


def test_create_note_writes_through_the_note_layer(
    vault: Path, state: RealPanelState
) -> None:
    response = post(
        ROUTE_MEMORY_SAVE,
        b"mode=create&name=plumber&title=Plumber&body=Call+Ana+at+9am.",
        state,
    )
    assert response.status == 303
    assert response.headers[0][0] == "Location"
    assert "note=plumber" in response.headers[0][1]
    path = vault / "notes" / "plumber.md"
    assert path.exists()
    # The file is what Claire's parser reads back: frontmatter included.
    note = notes_module.read_note("plumber", vault_dir=vault)
    assert note.title == "Plumber"
    assert "Call Ana at 9am." in note.text
    assert obsidian_module.validate_note_document(note.text) == []
    # The vault is marked as an Obsidian vault on first write.
    assert (vault / ".obsidian").is_dir()


def test_create_note_is_owner_only(vault: Path, state: RealPanelState) -> None:
    post(ROUTE_MEMORY_SAVE, b"mode=create&name=secret&body=x", state)
    mode = (vault / "notes" / "secret.md").stat().st_mode & 0o777
    assert mode == 0o600


def test_edit_updates_title_and_keeps_created(vault: Path, state: RealPanelState) -> None:
    post(
        ROUTE_MEMORY_SAVE,
        b"mode=edit&original=dentist&title=Dr+Ruiz&body=Now+Thursday.",
        state,
    )
    text = (vault / "notes" / "dentist.md").read_text(encoding="utf-8")
    assert "title: Dr Ruiz" in text
    assert "created: 2026-01-01" in text  # identity preserved
    assert "updated:" in text
    assert "Now Thursday." in text
    assert "Dr. Ruiz, Wednesday 16:00." not in text


def test_edit_refuses_a_note_that_does_not_exist(state: RealPanelState) -> None:
    response = post(ROUTE_MEMORY_SAVE, b"mode=edit&original=ghost&body=x", state)
    assert response.status == 400
    assert "was not found" in response.text


def test_create_refuses_to_clobber_an_existing_note(
    vault: Path, state: RealPanelState
) -> None:
    before = (vault / "notes" / "dentist.md").read_text(encoding="utf-8")
    response = post(ROUTE_MEMORY_SAVE, b"mode=create&name=dentist&body=gone", state)
    assert response.status == 400
    assert "already exists" in response.text
    assert (vault / "notes" / "dentist.md").read_text(encoding="utf-8") == before


def test_edit_keeps_frontmatter_keys_the_owner_added(
    vault: Path, state: RealPanelState
) -> None:
    """A key added by hand (Obsidian's `tags`, say) survives an edit on the page."""
    (vault / "notes" / "tagged.md").write_text(
        "---\ntitle: Tagged\ntags: health, home\n---\n\nold body\n", encoding="utf-8"
    )
    post(ROUTE_MEMORY_SAVE, b"mode=edit&original=tagged&body=new+body", state)
    text = (vault / "notes" / "tagged.md").read_text(encoding="utf-8")
    assert "tags: health, home" in text
    assert "new body" in text


def test_delete_removes_the_note_and_a_second_delete_is_refused(
    vault: Path, state: RealPanelState
) -> None:
    assert (vault / "notes" / "orphan.md").exists()
    first = post(ROUTE_MEMORY_DELETE, b"name=orphan", state)
    assert first.status == 303
    assert not (vault / "notes" / "orphan.md").exists()
    second = post(ROUTE_MEMORY_DELETE, b"name=orphan", state)
    assert second.status == 400
    assert "was not found" in second.text
    assert "Nothing was deleted" in second.text


def test_create_refuses_an_unsafe_name(vault: Path, state: RealPanelState) -> None:
    response = post(ROUTE_MEMORY_SAVE, b"mode=create&name=../escape&body=x", state)
    assert response.status == 400
    assert "not a usable note name" in response.text
    assert not (vault.parent / "escape.md").exists()


def test_written_note_round_trips_through_the_browser(
    vault: Path, state: RealPanelState
) -> None:
    """Write it on the page, read it on the page: the format is one format."""
    post(
        ROUTE_MEMORY_SAVE,
        b"mode=create&name=holiday&title=Holiday&body=Fly+on+the+3rd.+See+"
        b"[[dentist]].",
        state,
    )
    body = get(f"{ROUTE_MEMORY}?note=holiday", state).text
    assert "Fly on the 3rd." in body
    assert "See [[dentist]]" in body
    # The wiki link is shown as a link to the note that exists.
    assert f'{ROUTE_MEMORY}?note=dentist' in body


# ---------------------------------------------------------------------------
# The page's write guards still apply to the new routes
# ---------------------------------------------------------------------------


def test_memory_writes_require_a_loopback_origin(state: RealPanelState) -> None:
    for route, body in (
        (ROUTE_MEMORY_SAVE, b"mode=create&name=x&body=y"),
        (ROUTE_MEMORY_DELETE, b"name=orphan"),
    ):
        response = post(
            route,
            body,
            state,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status == 403, route
        assert "refused" in response.text


def test_memory_writes_refuse_a_credential_shaped_field(state: RealPanelState) -> None:
    response = post(
        ROUTE_MEMORY_SAVE, b"mode=create&name=x&body=y&api_key=abc", state
    )
    assert response.status == 403
    assert "never" in response.text


def test_memory_post_routes_reject_get(state: RealPanelState) -> None:
    for route in (ROUTE_MEMORY_SAVE, ROUTE_MEMORY_DELETE):
        response = get(route, state)
        assert response.status == 405, route


def test_oversized_note_body_is_refused_by_name(state: RealPanelState) -> None:
    body = b"mode=create&name=big&body=" + b"x" * (panel.MEMORY_WRITE_MAX_CHARS + 1)
    response = post(ROUTE_MEMORY_SAVE, body, state)
    assert response.status == 413
    assert "writes at most" in response.text


def test_a_body_the_panel_never_saw_is_not_written(
    vault: Path, state: RealPanelState
) -> None:
    post(ROUTE_MEMORY_SAVE, b"mode=create&body=no+name+here", state)
    assert not (vault / "notes" / ".md").exists()


# ---------------------------------------------------------------------------
# The index page points at the browser
# ---------------------------------------------------------------------------


def test_index_page_links_to_the_memories_browser(state: RealPanelState) -> None:
    body = get("/", state).text
    assert ROUTE_MEMORY in body
    assert "open the memories browser" in body
    assert "Memories" in body


def test_index_still_links_to_the_browser_with_no_notes_directory(
    tmp_path: Path,
) -> None:
    """The emptiest vault is exactly when the browser is most worth offering."""
    vault = tmp_path / "empty-vault"
    vault.mkdir()
    (vault / "CLAIRE.md").write_text("# Router\n", encoding="utf-8")
    state = RealPanelState(vault_dir=vault, diary_path=tmp_path / "claire.db")
    body = get("/", state).text
    assert "NotesDirectoryError" in body  # named, with the fix
    assert "The browser still opens" in body
    assert f'href="{ROUTE_MEMORY}"' in body

# ---------------------------------------------------------------------------
# Vault resolution: an injected router_path names its own vault
# ---------------------------------------------------------------------------

def test_injected_router_path_names_the_vault_without_vault_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``router_path`` must pin the vault to the router's directory.

    The router lives in the vault root by definition — ``resolve_router_path(v)``
    is exactly ``v / "CLAIRE.md"`` — so a state constructed with a ``router_path``
    and no ``vault_dir`` has already been told which vault it is looking at.
    Resolving ``$CLAIRE_VAULT_DIR`` / the config key / the built-in default
    instead shows the router and the notes from two different vaults at once,
    and in a test that second vault is the owner's real one.

    Non-vacuous: before the fix ``vault_path()`` fell through to the global
    vault, so ``CLAIRE_VAULT_DIR``'s decoy note appeared on the page and this
    test failed.  The global vault here is a ``tmp_path`` directory, so the real
    vault is never read.
    """
    router_dir = tmp_path / "injected-vault"
    (router_dir / "notes").mkdir(parents=True)
    (router_dir / "CLAIRE.md").write_text(
        "---\ntitle: injected router\n---\n\n# Pointers\n\n- here: notes/here.md\n",
        encoding="utf-8",
    )
    (router_dir / "notes" / "here.md").write_text(
        "---\ntitle: Here\n---\n\nThis note belongs to the injected vault.\n",
        encoding="utf-8",
    )

    # A stand-in for the machine-wide vault, reached through the same knobs the
    # real resolver uses.  Nothing under the owner's home directory is touched.
    global_vault = tmp_path / "global-vault"
    (global_vault / "notes").mkdir(parents=True)
    (global_vault / "CLAIRE.md").write_text("# Global router\n", encoding="utf-8")
    (global_vault / "notes" / "decoy.md").write_text(
        "---\ntitle: Decoy\n---\n\nGLOBAL-VAULT-DECOY\n", encoding="utf-8"
    )
    monkeypatch.setenv("CLAIRE_VAULT_DIR", str(global_vault))

    state = RealPanelState(
        router_path=router_dir / "CLAIRE.md",
        config_path=tmp_path / "config.json",
        diary_path=tmp_path / "claire.db",
        action_log_path=tmp_path / "actions.jsonl",
    )

    assert state.vault_path() == router_dir
    assert state.memory_notes().vault_dir == str(router_dir)

    body = get(ROUTE_MEMORY, state).text
    assert "here" in body
    assert "GLOBAL-VAULT-DECOY" not in body, body
    assert "decoy" not in body, body

def test_injected_router_path_reads_nothing_from_the_global_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serving the memories routes opens files under the injected router only.

    This is the read-level counterpart of the resolution assertion above: even
    if the two vaults happened to resolve alike, no global-vault file may be
    opened.  The first request proves the recorder sees the panel's reads at
    all, so the "nothing" assertions cannot pass by observing nothing.
    """
    router_dir = tmp_path / "injected-vault"
    _write_vault(router_dir)

    global_vault = tmp_path / "global-vault"
    _write_vault(global_vault)
    monkeypatch.setenv("CLAIRE_VAULT_DIR", str(global_vault))

    state = RealPanelState(
        router_path=router_dir / "CLAIRE.md",
        config_path=tmp_path / "config.json",
        diary_path=tmp_path / "claire.db",
        action_log_path=tmp_path / "actions.jsonl",
    )

    opened: list[str] = []
    real_open = Path.open

    def recording_open(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        opened.append(str(self))
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", recording_open)

    opened.clear()
    assert get(ROUTE_MEMORY, state).status == 200
    assert state.memory_notes().vault_dir == str(router_dir)
    assert opened, "the recorder did not observe the panel's own reads"
    assert all(str(router_dir) in path for path in opened), opened

def test_injected_vault_dir_outranks_the_router_parent(tmp_path: Path) -> None:
    """Two explicit paths: the router file is read, the vault_dir is the vault."""
    vault = tmp_path / "vault"
    _write_vault(vault)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "CLAIRE.md").write_text("# Not the vault\n", encoding="utf-8")

    state = RealPanelState(
        vault_dir=vault,
        router_path=elsewhere / "CLAIRE.md",
        config_path=tmp_path / "config.json",
        diary_path=tmp_path / "claire.db",
        action_log_path=tmp_path / "actions.jsonl",
    )
    assert state.vault_path() == vault
