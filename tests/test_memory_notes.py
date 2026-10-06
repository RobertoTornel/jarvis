"""Unit tests for :mod:`claire.memory.notes` — the note half of the router.

All tests use ``tmp_path``; the real vault is never touched (``CLAIRE_VAULT_DIR``
is monkeypatched away from the user's data directory where a test could
otherwise read or write real memory).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claire.memory import notes


@pytest.fixture()
def notes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated empty notes directory, with the vault env var redirected."""
    vault = tmp_path / "vault"
    directory = vault / "notes"
    directory.mkdir(parents=True)
    monkeypatch.setenv(notes.VAULT_ENV_VAR, str(vault))
    # Guard against the default pointing anywhere real.
    assert notes.resolve_notes_dir() == directory
    return directory


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Listing: name, path, byte size, title
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_list_notes_returns_name_path_size_and_title(notes_dir: Path) -> None:
    _write(
        notes_dir,
        "dentist.md",
        "---\ntitle: Dentist\n---\n\nDr. Ruiz, Wednesday 16:00.\n",
    )

    listed = notes.list_notes()

    assert len(listed) == 1
    info = listed[0]
    assert info.name == "dentist"
    assert info.path == notes_dir / "dentist.md"
    assert info.size_bytes == (notes_dir / "dentist.md").stat().st_size
    assert info.size_bytes > 0
    assert info.title == "Dentist"


@pytest.mark.unit
def test_list_notes_title_falls_back_to_first_heading_then_filename(
    notes_dir: Path,
) -> None:
    _write(notes_dir, "heading.md", "# Sailing lessons\n\nSaturdays.\n")
    _write(notes_dir, "no-title-at-all.md", "just prose, no heading\n")

    titles = {info.name: info.title for info in notes.list_notes()}

    assert titles["heading"] == "Sailing lessons"
    assert titles["no-title-at-all"] == "No title at all"


@pytest.mark.unit
def test_list_notes_is_sorted_and_ignores_non_markdown(notes_dir: Path) -> None:
    _write(notes_dir, "beta.md", "b\n")
    _write(notes_dir, "alpha.md", "a\n")
    _write(notes_dir, "notes.txt", "not a note\n")
    (notes_dir / "subdir").mkdir()
    _write(notes_dir / "subdir", "nested.md", "nested\n")

    names = [info.name for info in notes.list_notes()]

    assert names == ["alpha", "beta"]


@pytest.mark.unit
def test_list_notes_missing_directory_is_a_named_error(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "notes"

    with pytest.raises(notes.NotesDirectoryError) as excinfo:
        notes.list_notes(notes_dir=missing)

    message = str(excinfo.value)
    assert str(missing) in message
    assert "not found" in message.lower()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_read_note_returns_text_frontmatter_and_title(notes_dir: Path) -> None:
    _write(notes_dir, "car.md", "---\ntitle: Car service\nmileage: 90000\n---\n\nBooked.\n")

    note = notes.read_note("car")

    assert note.name == "car"
    assert note.title == "Car service"
    assert note.path == notes_dir / "car.md"
    assert note.size_bytes == (notes_dir / "car.md").stat().st_size
    assert note.frontmatter["mileage"] == "90000"
    assert "Booked." in note.body
    assert "title: Car service" not in note.body


@pytest.mark.unit
def test_read_note_accepts_the_md_suffix(notes_dir: Path) -> None:
    _write(notes_dir, "car.md", "# Car\n\nBooked.\n")

    assert notes.read_note("car.md").name == "car"


@pytest.mark.unit
def test_read_missing_note_raises_named_error_naming_note_and_directory(
    notes_dir: Path,
) -> None:
    with pytest.raises(notes.NoteNotFoundError) as excinfo:
        notes.read_note("ghost")

    message = str(excinfo.value)
    assert "ghost" in message
    assert str(notes_dir) in message
    assert str(notes_dir / "ghost.md") in message
    # Named error, never a silent empty result.
    assert isinstance(excinfo.value, FileNotFoundError)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_search_finds_term_in_title_and_reports_the_line(notes_dir: Path) -> None:
    _write(notes_dir, "dentist.md", "---\ntitle: Dentist appointment\n---\n\nWednesday.\n")
    _write(notes_dir, "groceries.md", "---\ntitle: Groceries\n---\n\nMilk and bread.\n")

    results = notes.search_notes("dentist")

    assert [match.name for match in results] == ["dentist"]
    match = results[0]
    assert match.matched_in == "title"
    assert match.line_no == 2  # the `title:` frontmatter line
    assert "Dentist appointment" in match.line
    assert match.score > 0


@pytest.mark.unit
def test_search_finds_term_in_body_and_reports_the_exact_line(notes_dir: Path) -> None:
    _write(
        notes_dir,
        "groceries.md",
        "---\ntitle: Groceries\n---\n\nMilk and bread.\nAlso saffron from the market.\n",
    )

    results = notes.search_notes("saffron")

    assert [match.name for match in results] == ["groceries"]
    match = results[0]
    assert match.matched_in == "body"
    assert match.line_no == 6
    assert match.line == "Also saffron from the market."


@pytest.mark.unit
def test_search_ranks_title_hits_above_body_hits_and_respects_limit(
    notes_dir: Path,
) -> None:
    _write(notes_dir, "alpha.md", "---\ntitle: Saffron\n---\n\nnothing else\n")
    _write(notes_dir, "beta.md", "---\ntitle: Spices\n---\n\nsaffron is a spice\n")

    results = notes.search_notes("saffron")

    assert [match.name for match in results] == ["alpha", "beta"]
    assert notes.search_notes("saffron", limit=1)[0].name == "alpha"


@pytest.mark.unit
def test_search_is_case_insensitive_and_empty_query_is_refused(
    notes_dir: Path,
) -> None:
    _write(notes_dir, "car.md", "---\ntitle: Car\n---\n\nOil change on Tuesday.\n")

    assert notes.search_notes("OIL CHANGE")[0].name == "car"
    assert notes.search_notes("oil")[0].name == "car"

    with pytest.raises(ValueError):
        notes.search_notes("   ")


@pytest.mark.unit
def test_search_returns_no_match_for_absent_term(notes_dir: Path) -> None:
    _write(notes_dir, "car.md", "---\ntitle: Car\n---\n\nOil change.\n")

    assert notes.search_notes("quantumchromodynamics") == []


# --------------------------------------------------------------------------
# Router honesty: unreferenced notes
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_unreferenced_notes_reports_notes_the_router_never_mentions(
    notes_dir: Path,
) -> None:
    _write(notes_dir, "dentist.md", "---\ntitle: Dentist\n---\n\nWednesday.\n")
    _write(notes_dir, "car.md", "---\ntitle: Car\n---\n\nOil change.\n")
    router = (
        "---\ntitle: Claire memory router\nrole: router\nalways_loaded: true\n"
        "budget_bytes: 6000\n---\n\n"
        "# Pointers\n"
        "- dentist: notes/dentist.md — Dr. Ruiz, Wednesday 16:00.\n"
    )

    unreferenced = notes.unreferenced_notes(router)

    assert [info.name for info in unreferenced] == ["car"]


@pytest.mark.unit
def test_unreferenced_notes_ignores_frontmatter_title_and_word_prefixes(
    notes_dir: Path,
) -> None:
    _write(notes_dir, "car.md", "---\ntitle: Car\n---\n\nOil change.\n")
    _write(notes_dir, "nameserver.md", "---\ntitle: Nameserver\n---\n\nDNS.\n")
    router = (
        "---\ntitle: car\nrole: router\n---\n\n"
        "Only the nameserver thing is listed.\n"
    )

    unreferenced = notes.unreferenced_notes(router)

    assert [info.name for info in unreferenced] == ["car"]


@pytest.mark.unit
def test_unreferenced_notes_does_not_write_anything(notes_dir: Path) -> None:
    _write(notes_dir, "car.md", "---\ntitle: Car\n---\n\nOil change.\n")
    before = sorted(p.name for p in notes_dir.iterdir())

    notes.unreferenced_notes("# Pointers\n(nothing yet)\n")

    assert sorted(p.name for p in notes_dir.iterdir()) == before


@pytest.mark.unit
def test_unreferenced_notes_reading_a_missing_router_is_a_named_error(
    notes_dir: Path,
) -> None:
    _write(notes_dir, "car.md", "---\ntitle: Car\n---\n\nOil change.\n")
    missing_router = notes_dir.parent / "CLAIRE.md"

    with pytest.raises(notes.RouterNotFoundError) as excinfo:
        notes.unreferenced_notes(router_path=missing_router)

    assert str(missing_router) in str(excinfo.value)


# --------------------------------------------------------------------------
# Creating notes
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_create_note_writes_markdown_with_frontmatter(notes_dir: Path) -> None:
    info = notes.create_note("dentist", "Dr. Ruiz, Wednesday 16:00.")

    assert info.name == "dentist"
    assert info.path == notes_dir / "dentist.md"
    text = (notes_dir / "dentist.md").read_text(encoding="utf-8")
    assert text.startswith("---\ntitle: Dentist\n")
    assert "created: " in text
    assert text.rstrip().endswith("Dr. Ruiz, Wednesday 16:00.")
    # Round-trips through the reader, and shows up in listings.
    assert notes.read_note("dentist").title == "Dentist"
    assert [item.name for item in notes.list_notes()] == ["dentist"]
    assert notes.search_notes("Ruiz")[0].name == "dentist"


@pytest.mark.unit
def test_create_note_honours_an_explicit_title(notes_dir: Path) -> None:
    notes.create_note("trip", "Body.", title="Trip to Lisbon")

    assert notes.read_note("trip").title == "Trip to Lisbon"


@pytest.mark.unit
def test_create_note_creates_a_missing_notes_directory(tmp_path: Path) -> None:
    target = tmp_path / "vault" / "notes"

    info = notes.create_note("first", "hello", notes_dir=target)

    assert info.path == target / "first.md"
    assert info.path.exists()


@pytest.mark.unit
def test_create_note_refuses_to_overwrite_unless_told(notes_dir: Path) -> None:
    notes.create_note("car", "First.")
    original = (notes_dir / "car.md").read_text(encoding="utf-8")

    with pytest.raises(notes.NoteExistsError) as excinfo:
        notes.create_note("car", "Second.")

    message = str(excinfo.value)
    assert "car" in message
    assert str(notes_dir / "car.md") in message
    assert (notes_dir / "car.md").read_text(encoding="utf-8") == original

    info = notes.create_note("car", "Second.", overwrite=True)
    assert info.name == "car"
    assert "Second." in (notes_dir / "car.md").read_text(encoding="utf-8")


@pytest.mark.unit
def test_create_note_rejects_path_traversal_and_writes_nothing_outside_the_vault(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    directory = vault / "notes"
    directory.mkdir(parents=True)
    # A plausible target outside the vault that must stay absent.
    outside = tmp_path / "etc" / "passwd"

    for bad in ("../../etc/passwd", "../escape", "/etc/passwd", "..", "sub/dir", "a\\b"):
        with pytest.raises(notes.InvalidNoteNameError):
            notes.create_note(bad, "pwned")

    assert not outside.exists()
    assert not (tmp_path / "etc").exists()
    assert not (tmp_path / "escape.md").exists()
    assert list(directory.iterdir()) == []
    # And the traversal paths cannot be read either.
    with pytest.raises(notes.InvalidNoteNameError):
        notes.read_note("../../etc/passwd")


@pytest.mark.unit
def test_create_note_quotes_a_title_containing_a_colon(notes_dir: Path) -> None:
    notes.create_note("dentist", "Body.", title="Dentist: Dr. Ruiz")

    assert notes.read_note("dentist").title == "Dentist: Dr. Ruiz"


@pytest.mark.unit
def test_search_reports_a_heading_title_hit_on_the_heading_line(
    notes_dir: Path,
) -> None:
    _write(notes_dir, "sailing.md", "Some preamble.\n\n# Sailing lessons\n\nSaturdays.\n")

    match = notes.search_notes("sailing")[0]

    assert match.matched_in == "title"
    assert match.line_no == 3
    assert match.line == "# Sailing lessons"


@pytest.mark.unit
def test_a_symlinked_note_cannot_reach_outside_the_vault(
    tmp_path: Path, notes_dir: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("SECRET OUTSIDE\n", encoding="utf-8")
    (notes_dir / "evil.md").symlink_to(outside)

    # Not listed, not readable, not writable — and the target is untouched.
    assert notes.list_notes() == []
    with pytest.raises(notes.InvalidNoteNameError):
        notes.read_note("evil")
    with pytest.raises(notes.InvalidNoteNameError):
        notes.create_note("evil", "pwned", overwrite=True)
    assert outside.read_text(encoding="utf-8") == "SECRET OUTSIDE\n"


@pytest.mark.unit
def test_create_note_writes_a_body_that_already_has_frontmatter_verbatim(
    notes_dir: Path,
) -> None:
    document = "---\ntitle: Mine\n---\n\nBody.\n"

    notes.create_note("verbatim", document)

    assert (notes_dir / "verbatim.md").read_text(encoding="utf-8") == document
    assert notes.read_note("verbatim").title == "Mine"


@pytest.mark.unit
def test_parse_frontmatter_and_strip_frontmatter_edges() -> None:
    meta, body = notes.parse_frontmatter("---\ntitle: \"Quoted: yes\"\n---\nbody\n")
    assert meta["title"] == "Quoted: yes"
    assert body == "body\n"

    # No frontmatter, and an unterminated block, are both plain body text.
    assert notes.parse_frontmatter("plain text") == ({}, "plain text")
    assert notes.parse_frontmatter("---\ntitle: nope\n") == ({}, "---\ntitle: nope\n")
    assert notes.parse_frontmatter("") == ({}, "")


@pytest.mark.unit
def test_resolve_locations_default_to_the_user_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "custom-vault"
    monkeypatch.setenv(notes.VAULT_ENV_VAR, str(vault))

    assert notes.resolve_vault_dir() == vault
    assert notes.resolve_notes_dir() == vault / "notes"
    assert notes.resolve_router_path() == vault / "CLAIRE.md"

    monkeypatch.delenv(notes.VAULT_ENV_VAR)
    assert notes.resolve_vault_dir() == (
        Path.home() / ".local" / "share" / "claire" / "vault"
    )
    # An explicit argument always wins over the environment.
    assert notes.resolve_vault_dir(tmp_path / "explicit") == tmp_path / "explicit"
    assert notes.ROUTER_BUDGET_BYTES == 6000
