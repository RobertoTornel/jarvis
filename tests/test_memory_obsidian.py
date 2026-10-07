"""Unit tests for the Obsidian-shaped vault and its configuration.

"Move the memories to Obsidian" means a genuine Obsidian-compatible markdown
vault: a folder of notes with frontmatter Obsidian parses, ``[[wiki links]]``
between them, and a location the owner can point Obsidian at without editing
source.  These tests pin that shape, the wiki-link rules, and the resolution
order for the vault path (explicit argument, then ``CLAIRE_VAULT_DIR``, then the
``vault_dir`` config key, then the default).

Nothing here touches a real vault: every test works inside ``tmp_path``.

The gate is ``pytest tests/ -m unit`` and it SILENTLY DESELECTS a file with no
unit marker — hence ``pytestmark = pytest.mark.unit`` below.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claire.config import load_settings
from claire.memory import notes, obsidian

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_ambient_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may resolve the owner's real vault by accident."""
    monkeypatch.delenv(notes.VAULT_ENV_VAR, raising=False)
    notes._configured_vault_dir_at.cache_clear()


# ---------------------------------------------------------------------------
# Vault shape
# ---------------------------------------------------------------------------


def test_ensure_vault_creates_notes_and_obsidian_folder(tmp_path: Path) -> None:
    vault = tmp_path / "claire-vault"
    layout = obsidian.ensure_vault(vault)
    assert (vault / "notes").is_dir()
    assert (vault / ".obsidian").is_dir()
    assert layout.notes_exists and layout.vault_exists
    assert layout.is_obsidian_vault
    assert not layout.router_exists  # the router has its own authority
    assert (vault / "CLAIRE.md").exists() is False


def test_ensure_vault_is_idempotent_and_never_destroys(tmp_path: Path) -> None:
    vault = tmp_path / "claire-vault"
    obsidian.ensure_vault(vault)
    marker = vault / "notes" / "keep.md"
    marker.write_text("---\ntitle: Keep\n---\n\nhello\n", encoding="utf-8")
    (vault / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    second = obsidian.ensure_vault(vault)
    assert second.notes_exists and second.obsidian_dir_exists
    assert marker.read_text(encoding="utf-8").endswith("hello\n")
    assert (vault / ".obsidian" / "app.json").read_text(encoding="utf-8") == "{}"


def test_ensure_vault_can_skip_marking_the_folder(tmp_path: Path) -> None:
    vault = tmp_path / "bare"
    obsidian.ensure_vault(vault, mark_as_obsidian=False)
    assert (vault / "notes").is_dir()
    assert not (vault / ".obsidian").exists()
    assert not obsidian.is_obsidian_vault(vault)


def test_describe_vault_names_what_is_missing(tmp_path: Path) -> None:
    vault = tmp_path / "nothing-yet"
    layout = obsidian.describe_vault(vault)
    assert not layout.vault_exists
    assert not layout.notes_exists
    assert not layout.router_exists
    assert not layout.is_well_formed
    assert layout.router_path == vault / notes.ROUTER_FILENAME
    assert layout.notes_dir == vault / notes.NOTES_DIRNAME


def test_well_formed_does_not_require_obsidian_settings(tmp_path: Path) -> None:
    """Obsidian creates ``.obsidian/`` itself; a plain folder is still a vault."""
    vault = tmp_path / "plain"
    (vault / "notes").mkdir(parents=True)
    (vault / "CLAIRE.md").write_text("# Router\n", encoding="utf-8")
    layout = obsidian.describe_vault(vault)
    assert layout.is_well_formed
    assert not layout.obsidian_dir_exists


def test_is_obsidian_vault_detects_the_settings_folder(tmp_path: Path) -> None:
    vault = tmp_path / "marked"
    (vault / ".obsidian").mkdir(parents=True)
    assert obsidian.is_obsidian_vault(vault)
    assert not obsidian.is_obsidian_vault(tmp_path / "unmarked")
    assert not obsidian.is_obsidian_vault(vault / "notes")


# ---------------------------------------------------------------------------
# Wiki links
# ---------------------------------------------------------------------------


def test_wiki_link_uses_the_note_name() -> None:
    assert obsidian.wiki_link("dentist") == "[[dentist]]"
    assert obsidian.wiki_link("dentist", "the dentist") == "[[dentist|the dentist]]"


def test_wiki_link_refuses_a_name_that_is_not_a_note() -> None:
    with pytest.raises(notes.InvalidNoteNameError):
        obsidian.wiki_link("../etc/passwd")


def test_extract_wiki_links_strips_alias_and_heading() -> None:
    text = "See [[dentist|Dr Ruiz]], [[car-service#service]] and [[plain]]."
    assert obsidian.extract_wiki_links(text) == ["dentist", "car-service", "plain"]
    assert obsidian.extract_wiki_links("") == []


def test_link_targets_in_text_reads_both_spellings() -> None:
    text = "- dentist: [[dentist]]\n- car: notes/car-service.md\n"
    assert obsidian.link_targets_in_text(text) == ["dentist", "car-service"]
    # Duplicates collapse, first-seen order wins.
    assert obsidian.link_targets_in_text("[[a]] and [[a]] and notes/a.md") == ["a"]


def test_resolve_wiki_link_is_case_insensitive_and_reports_misses() -> None:
    known = ["dentist", "car-service"]
    assert obsidian.resolve_wiki_link("DENTIST", known) == "dentist"
    assert obsidian.resolve_wiki_link("dentist|alias", known) == "dentist"
    assert obsidian.resolve_wiki_link("ghost", known) is None
    assert obsidian.resolve_wiki_link("", known) is None


# ---------------------------------------------------------------------------
# Frontmatter Obsidian will parse
# ---------------------------------------------------------------------------


def test_validate_accepts_the_frontmatter_claire_writes() -> None:
    document = (
        "---\n"
        "title: Dentist\n"
        "created: 2026-01-01\n"
        "updated: 2026-02-02\n"
        "tags: health, home\n"
        "---\n\n"
        "Dr. Ruiz, Wednesday 16:00.\n"
    )
    assert obsidian.validate_note_document(document) == []


def test_validate_accepts_a_note_with_no_frontmatter() -> None:
    assert obsidian.validate_note_document("Just a body.\n") == []


def test_validate_reports_an_unclosed_block() -> None:
    problems = obsidian.validate_note_document("---\ntitle: Never closed\n\nbody\n")
    assert problems and "never closed by a matching" in problems[0]


def test_validate_reports_a_tab() -> None:
    problems = obsidian.validate_note_document("---\ntitle:\tX\n---\n\nbody\n")
    assert any("tab" in problem for problem in problems)


def test_validate_reports_a_non_pair_line() -> None:
    problems = obsidian.validate_note_document("---\ntitle: X\nnot a pair\n---\nbody\n")
    assert any("key: value" in problem for problem in problems)


def test_validate_reports_duplicate_keys() -> None:
    problems = obsidian.validate_note_document("---\ntitle: X\ntitle: Y\n---\nbody\n")
    assert any("duplicate" in problem for problem in problems)


def test_validate_reports_binary_and_empty_input() -> None:
    assert any("NUL" in problem for problem in obsidian.validate_note_document("a\x00b"))
    assert obsidian.validate_note_document(None)


def test_created_and_updated_notes_are_valid_obsidian_documents(tmp_path: Path) -> None:
    """The writer and the validator agree — including on an edit."""
    vault = tmp_path / "vault"
    notes.create_note(
        "dentist",
        "Dr. Ruiz. See [[car-service]].",
        vault_dir=vault,
        today="2026-01-01",
    )
    notes.update_note("dentist", "Now Thursday.", vault_dir=vault, today="2026-02-02")
    document = (vault / "notes" / "dentist.md").read_text(encoding="utf-8")
    assert obsidian.validate_note_document(document) == []
    assert "created: 2026-01-01" in document
    assert "updated: 2026-02-02" in document
    assert notes.unreferenced_notes(
        "see notes/dentist.md", notes_dir=vault / "notes", vault_dir=vault
    ) == []


def test_vault_conventions_describe_the_layout() -> None:
    lines = obsidian.vault_conventions()
    assert lines and all(isinstance(line, str) and line for line in lines)
    joined = " ".join(lines)
    assert "CLAIRE.md" in joined and "notes/" in joined and "[[note name]]" in joined


# ---------------------------------------------------------------------------
# Where the vault lives: argument, then environment, then config, then default
# ---------------------------------------------------------------------------


def _config_file(tmp_path: Path, vault: str) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"vault_dir": vault}), encoding="utf-8")
    return path


def test_explicit_argument_outranks_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(notes.VAULT_ENV_VAR, str(tmp_path / "from-env"))
    assert notes.resolve_vault_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_environment_outranks_the_config_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(_config_file(tmp_path, str(tmp_path / "from-config"))))
    monkeypatch.setenv(notes.VAULT_ENV_VAR, str(tmp_path / "from-env"))
    notes._configured_vault_dir_at.cache_clear()
    assert notes.resolve_vault_dir() == tmp_path / "from-env"


def test_config_key_is_used_when_the_environment_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "obsidian" / "Claire"
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(_config_file(tmp_path, str(configured))))
    notes._configured_vault_dir_at.cache_clear()
    assert notes.resolve_vault_dir() == configured
    assert notes.resolve_notes_dir() == configured / "notes"
    assert notes.resolve_router_path() == configured / "CLAIRE.md"


def test_config_key_expands_a_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(_config_file(tmp_path, "~/Obsidian/Claire")))
    notes._configured_vault_dir_at.cache_clear()
    assert notes.resolve_vault_dir() == Path.home() / "Obsidian" / "Claire"


def test_default_is_the_claire_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "no-config.json"
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(missing))
    notes._configured_vault_dir_at.cache_clear()
    assert notes.resolve_vault_dir() == (
        Path.home() / ".local" / "share" / "claire" / "vault"
    )


def test_a_broken_config_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "config.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(broken))
    notes._configured_vault_dir_at.cache_clear()
    assert notes.resolve_vault_dir().name == "vault"


def test_load_settings_exposes_an_expanded_vault_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "vaults" / "Claire"
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"vault_dir": "~/Obsidian/Claire", "db_path": str(tmp_path / "c.db")}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(path))
    settings = load_settings()
    assert settings.vault_dir == str(Path.home() / "Obsidian" / "Claire")
    assert configured.name == "Claire"  # sanity: the expectation is a real path


def test_default_config_key_is_portable() -> None:
    """The shipped default must not contain one machine's home directory."""
    from claire.config import get_default_config

    assert get_default_config()["vault_dir"] == "~/.local/share/claire/vault"


# ---------------------------------------------------------------------------
# Creating and deleting notes in a vault that does not exist yet
# ---------------------------------------------------------------------------


def test_ensure_notes_dir_creates_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "fresh"
    resolved = notes.ensure_notes_dir(vault_dir=vault)
    assert resolved == vault / "notes" and resolved.is_dir()


def test_first_note_creates_the_vault_and_obsidian_marker(tmp_path: Path) -> None:
    from claire.memory import router as router_module

    vault = tmp_path / "fresh"
    notes.create_note("dentist", "Dr. Ruiz.", vault_dir=vault)
    assert (vault / "notes" / "dentist.md").is_file()
    obsidian.ensure_vault(vault)
    assert obsidian.is_obsidian_vault(vault)
    layout = obsidian.describe_vault(vault)
    assert layout.notes_exists and layout.obsidian_dir_exists
    # Writing a note does not invent a router: that is ensure_router's job, so
    # there is one authority for the template and its budget.
    assert not layout.router_exists
    assert not layout.is_well_formed
    router_module.ensure_router(vault_dir=vault)
    assert obsidian.describe_vault(vault).is_well_formed


def test_delete_note_refuses_a_missing_note(tmp_path: Path) -> None:
    vault = tmp_path / "fresh"
    notes.ensure_notes_dir(vault_dir=vault)
    with pytest.raises(notes.NoteNotFoundError):
        notes.delete_note("ghost", vault_dir=vault)


def test_update_note_refuses_a_missing_note(tmp_path: Path) -> None:
    vault = tmp_path / "fresh"
    notes.ensure_notes_dir(vault_dir=vault)
    with pytest.raises(notes.NoteNotFoundError):
        notes.update_note("ghost", "x", vault_dir=vault)


def test_update_note_can_replace_a_whole_document(tmp_path: Path) -> None:
    """A body that already carries frontmatter is written verbatim."""
    vault = tmp_path / "vault"
    notes.create_note("dentist", "old", vault_dir=vault, today="2026-01-01")
    replacement = "---\ntitle: Replaced\ntags: home\n---\n\nnew body\n"
    notes.update_note("dentist", replacement, vault_dir=vault)
    path = vault / "notes" / "dentist.md"
    assert path.read_text(encoding="utf-8") == replacement
    assert obsidian.validate_note_document(path.read_text(encoding="utf-8")) == []
