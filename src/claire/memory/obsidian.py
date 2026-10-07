"""An Obsidian-compatible vault — the *shape* of Claire's memory directory.

An Obsidian vault is not an application format.  It is a directory of markdown
files, optionally carrying a ``.obsidian/`` folder of app settings, and it is
opened by *pointing Obsidian at the folder*.  That is the whole story, and it is
why "move the memories to Obsidian" needs no Obsidian installed, no plugin, no
database and no import: Claire's vault is already markdown, so the work is to
make its structure and conventions *deliberately* vault-shaped and to keep them
that way.

This module owns that shape; :mod:`claire.memory.notes` still owns reading and
writing one note.  Nothing here imports Obsidian, launches it, or assumes it is
installed — the layout is useful on its own (a markdown tool, ``grep``, a phone
through Syncthing, or the panel's memories browser).

The layout::

    <vault>/                     ← point Obsidian at THIS directory
    ├── CLAIRE.md                ← the router: always loaded, holds pointers
    ├── .obsidian/               ← app settings, created by Obsidian itself
    └── notes/                   ← one flat file per fact

Conventions, all chosen because Obsidian reads them natively:

* **Frontmatter is flat ``key: value``.**  That subset is valid YAML, so
  Obsidian's Properties panel shows the keys instead of rendering the block as
  text.  Keys Claire writes are ``title``, ``created`` and ``updated``; a list
  key such as ``tags`` is written comma-separated (``tags: home, errands``),
  which is the one list syntax Obsidian reads as a list.
* **One note is one file, and the filename is the note's identity.**  Obsidian
  resolves ``[[dentist]]`` against filenames, so a note's ``title`` is a label
  and the filename is the link target.
* **Pointers from the router are wiki links.**  ``[[dentist]]`` is what an
  Obsidian user expects and what the graph view draws.  A pointer may also be
  written as the plain path ``notes/dentist.md``; both forms count as "the
  router mentions this note" for
  :func:`claire.memory.notes.unreferenced_notes`, because a vault that grew up
  before wiki links existed must not suddenly read as orphaned.
* **Links between notes are wiki links too**, and :func:`extract_wiki_links`
  reads them so the panel can show which of them actually resolve.

Safety model, unchanged by any of this: the vault holds the owner's facts and
Claire's instructions to herself, so it lives OUTSIDE the repository (default
``~/.local/share/claire/vault``, or the ``vault_dir`` config key, or
``$CLAIRE_VAULT_DIR``), it is written at mode 0600, and nothing in this module
ever writes *inside* a repository checkout.  See ``docs/OBSIDIAN.md``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .notes import (
    NOTES_DIRNAME,
    ROUTER_FILENAME,
    normalize_note_name,
    resolve_notes_dir,
    resolve_router_path,
    resolve_vault_dir,
)

__all__ = [
    "OBSIDIAN_DIRNAME",
    "VaultLayout",
    "describe_vault",
    "ensure_vault",
    "extract_wiki_links",
    "is_obsidian_vault",
    "link_targets_in_text",
    "resolve_wiki_link",
    "validate_note_document",
    "vault_conventions",
    "wiki_link",
]

#: Folder Obsidian keeps its own settings in.  Its presence is what makes a
#: directory a *recognised* vault rather than merely an openable one.
OBSIDIAN_DIRNAME = ".obsidian"

#: ``[[target]]``, ``[[target|label]]`` and ``[[target#heading]]``.
_WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")

#: A bare markdown link to a note file, e.g. ``notes/dentist.md``.
_NOTE_PATH_RE = re.compile(
    r"(?:^|[\s(\[])(?P<path>(?:\./)?notes/(?P<name>[^/\s\)\]]+)\.md)(?=$|[\s)\].#,])"
)

#: A safe frontmatter key for the flat subset Claire and Obsidian both read.
_FRONTMATTER_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_FENCE = "---"


@dataclass(frozen=True)
class VaultLayout:
    """Where the pieces of the vault are, and which of them exist right now.

    Existence is reported per piece rather than as one ``exists`` flag: "the
    vault is there but ``notes/`` is not" is exactly the state a half-finished
    install is in, and it must be distinguishable from "nothing is there".
    """

    vault_dir: Path
    notes_dir: Path
    router_path: Path
    obsidian_dir: Path
    vault_exists: bool
    notes_exists: bool
    router_exists: bool
    obsidian_dir_exists: bool

    @property
    def is_obsidian_vault(self) -> bool:
        """True when Obsidian has already marked this folder as a vault."""
        return self.vault_exists and self.obsidian_dir_exists

    @property
    def is_well_formed(self) -> bool:
        """True when the vault has the shape Obsidian needs to open it.

        ``.obsidian/`` is deliberately **not** required: Obsidian creates it on
        first open, and requiring it would call a perfectly good markdown folder
        malformed.
        """
        return self.vault_exists and self.notes_exists and self.router_exists


def describe_vault(
    vault_dir: Optional[os.PathLike] = None,
) -> VaultLayout:
    """Report the vault layout without creating, reading or changing anything."""
    vault = resolve_vault_dir(vault_dir)
    notes = resolve_notes_dir(vault_dir=vault_dir)
    router = resolve_router_path(vault_dir=vault_dir)
    return VaultLayout(
        vault_dir=vault,
        notes_dir=notes,
        router_path=router,
        obsidian_dir=vault / OBSIDIAN_DIRNAME,
        vault_exists=vault.is_dir(),
        notes_exists=notes.is_dir(),
        router_exists=router.is_file(),
        obsidian_dir_exists=(vault / OBSIDIAN_DIRNAME).is_dir(),
    )


def is_obsidian_vault(path: os.PathLike) -> bool:
    """True when *path* is a directory Obsidian has already opened as a vault."""
    candidate = Path(path).expanduser()
    return candidate.is_dir() and (candidate / OBSIDIAN_DIRNAME).is_dir()


def ensure_vault(
    vault_dir: Optional[os.PathLike] = None,
    *,
    mark_as_obsidian: bool = True,
) -> VaultLayout:
    """Create the vault's directories so Obsidian can open it; return the layout.

    Creates the vault directory, ``notes/``, and (unless *mark_as_obsidian* is
    false) an empty ``.obsidian/`` folder.  An empty settings folder is enough
    for Obsidian to recognise the directory as a vault immediately; it fills the
    folder with its own configuration on first open, and this function never
    writes a setting of its own — inventing app preferences for the owner would
    be a change they did not ask for.

    Existing directories are left exactly as they are: this is idempotent and
    never deletes, moves or rewrites anything.  The router file is deliberately
    **not** created here — that is ``claire.memory.router.ensure_router()``'s
    job, so there is one authority for the router's template and budget.
    """
    vault = resolve_vault_dir(vault_dir)
    notes = resolve_notes_dir(vault_dir=vault_dir)
    notes.mkdir(parents=True, exist_ok=True)
    if mark_as_obsidian:
        (vault / OBSIDIAN_DIRNAME).mkdir(parents=True, exist_ok=True)
    return describe_vault(vault_dir)


# ---------------------------------------------------------------------------
# Wiki links: the one linking convention Obsidian users expect
# ---------------------------------------------------------------------------


def wiki_link(name: str, label: Optional[str] = None) -> str:
    """Render ``[[name]]`` (or ``[[name|label]]``) for a note.

    The name is normalised with the same :func:`normalize_note_name` the reader
    and writer use, so a link can never be generated that points outside the
    notes directory — :class:`InvalidNoteNameError` is raised instead.
    """
    target = normalize_note_name(name)
    if label:
        return f"[[{target}|{label}]]"
    return f"[[{target}]]"


def _link_target(inner: str) -> str:
    """The note name inside a wiki link, without its alias or heading."""
    target = inner.split("|", 1)[0]
    target = target.split("#", 1)[0]
    return target.strip()


def extract_wiki_links(text: str) -> List[str]:
    """Every wiki-link target in *text*, in order, duplicates preserved.

    ``[[dentist|the dentist]]`` yields ``dentist``: the alias is display text,
    not a target.  Returning the raw order (rather than a set) keeps this usable
    for showing a note's links as the author wrote them.
    """
    return [_link_target(match) for match in _WIKI_LINK_RE.findall(text or "")]


def link_targets_in_text(text: str) -> List[str]:
    """Every *note reference* in *text*: wiki links and ``notes/<name>.md`` paths.

    Both forms are returned as bare note names so a caller can compare them with
    :func:`claire.memory.notes.list_notes` without knowing which spelling the
    author used.  Duplicates are collapsed, first-seen order preserved.  This is
    what makes the router's pointers inspectable: a pointer is a reference to a
    note, however it is spelled.
    """
    seen: Dict[str, None] = {}
    for target in extract_wiki_links(text):
        if target:
            seen.setdefault(target, None)
    for match in _NOTE_PATH_RE.finditer(text or ""):
        name = match.group("name").strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def resolve_wiki_link(target: str, known_names: Iterable[str]) -> Optional[str]:
    """Match a link target to a known note name, case-insensitively.

    Returns the *known* spelling, or ``None`` for a link that points at no note
    — an unresolved link is reported, never silently "fixed" by creating a note.
    Obsidian itself is case-insensitive about filenames, so this is too.
    """
    wanted = _link_target(str(target or "")).strip().casefold()
    if not wanted:
        return None
    for name in known_names:
        if str(name).casefold() == wanted:
            return str(name)
    return None


# ---------------------------------------------------------------------------
# Frontmatter that Obsidian will parse rather than print
# ---------------------------------------------------------------------------


def validate_note_document(text: str) -> List[str]:
    """Report why *text* is not a well-formed Obsidian markdown note.

    Returns a list of human-readable problems; an empty list means the document
    is fine.  "Fine" is narrow on purpose: the checker understands the flat
    ``key: value`` frontmatter subset this codebase writes (no nested maps, no
    block scalars), so it never claims to validate YAML in general.  Obsidian
    renders the block as a Properties table only when it parses; malformed
    frontmatter shows up as literal ``---`` text, which is exactly the rot this
    catches before it reaches the owner's screen.
    """
    problems: List[str] = []
    if text is None:
        return ["the document is empty (None), so it has no note body."]
    if "\x00" in text:
        problems.append("the document contains a NUL byte, which is not text.")

    lines = text.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != _FENCE:
        # No frontmatter at all is legal markdown, and Obsidian is happy with
        # it; there is simply nothing to validate.
        return problems

    end = None
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == _FENCE:
            end = index
            break
    if end is None:
        problems.append(
            "frontmatter opens with '---' but is never closed by a matching "
            "'---' line, so Obsidian renders it as literal text."
        )
        return problems

    keys: List[str] = []
    for offset, raw in enumerate(lines[start + 1 : end], start=2):
        line = raw.rstrip()
        if not line.strip():
            continue
        if "\t" in line:
            problems.append(
                f"line {offset} of the frontmatter contains a tab; YAML forbids "
                f"tab indentation."
            )
            continue
        if line.lstrip() != line:
            problems.append(
                f"line {offset} of the frontmatter is indented; the flat "
                f"key: value subset does not nest."
            )
            continue
        key, separator, _value = line.partition(":")
        if not separator or not _FRONTMATTER_KEY_RE.match(key.strip()):
            problems.append(
                f"line {offset} of the frontmatter is not a 'key: value' pair "
                f"({line.strip()!r})."
            )
            continue
        keys.append(key.strip())
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        problems.append(
            "duplicate frontmatter key(s): " + ", ".join(duplicates) + "."
        )
    return problems


def vault_conventions() -> Tuple[str, ...]:
    """The conventions above, as short lines a page or a report can print.

    Kept here rather than in the panel so the panel, the tests and the docs all
    quote one authority instead of three that drift.
    """
    return (
        "the vault is a directory of markdown files; point Obsidian at it",
        f"the always-loaded router is {ROUTER_FILENAME} at the vault root",
        f"one note is one file under {NOTES_DIRNAME}/, named by its filename",
        "frontmatter is flat 'key: value' (title, created, updated, tags)",
        "a pointer or cross-reference is a wiki link: [[note name]]",
    )
