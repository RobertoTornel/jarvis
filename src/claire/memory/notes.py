"""Note files — the other half of "a router, not a database".

Claire's always-loaded router (``CLAIRE.md``) is HARD-CAPPED (default 6000
bytes) and only holds *pointers*.  Anything bigger than a line lives in a
**note**: a plain markdown file under the vault's ``notes/`` directory, with
simple frontmatter::

    ---
    title: Dentist
    created: 2026-01-31
    ---

    Dr. Ruiz, Wednesday 16:00, Calle Mayor 4.

This module is deliberately tiny and dependency-free (no PyYAML, no new
packages): it parses only the ``key: value`` subset of frontmatter that Claire
herself writes, searches notes cheaply by substring/token overlap, and reports
which notes the router body does not mention — so the router can be kept
honest.

Nothing here touches the conversation history or the LLM: a note costs tokens
only when it is actually read.  Memory content is Claire's own instructions to
herself and lives OUTSIDE the repository, in the user's data directory
(``~/.local/share/claire/vault`` by default).

The vault location is configurable, and resolution order is deliberate: an
explicit *vault_dir* argument wins, then ``$CLAIRE_VAULT_DIR``, then the
``vault_dir`` key in Claire's ``config.json``, then the built-in default.  The
config key makes the vault movable into an owner's own Obsidian setup without
touching the environment; ``docs/OBSIDIAN.md`` covers the layout and
:mod:`claire.memory.obsidian` owns vault-shaped helpers.

Every failure is NAMED: there is no code path that returns a silent empty
result for a missing file or directory.  See ``docs/MEMORY.md`` for the owner's
guide.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

__all__ = [
    "NOTES_DIRNAME",
    "ROUTER_BUDGET_BYTES",
    "ROUTER_FILENAME",
    "InvalidNoteNameError",
    "Note",
    "NoteExistsError",
    "NoteInfo",
    "NoteMatch",
    "NoteNotFoundError",
    "NotesDirectoryError",
    "RouterNotFoundError",
    "create_note",
    "delete_note",
    "ensure_notes_dir",
    "extract_title",
    "list_notes",
    "normalize_note_name",
    "parse_frontmatter",
    "read_note",
    "render_frontmatter",
    "resolve_notes_dir",
    "resolve_router_path",
    "resolve_vault_dir",
    "search_notes",
    "strip_frontmatter",
    "unreferenced_notes",
    "update_note",
]

#: Environment variable that overrides where the vault lives.
VAULT_ENV_VAR = "CLAIRE_VAULT_DIR"

#: Directory name of the note vault inside the user's Claire data directory.
VAULT_DIRNAME = "vault"

#: Notes live here, under the vault.
NOTES_DIRNAME = "notes"

#: The always-loaded router file, at the top of the vault.
ROUTER_FILENAME = "CLAIRE.md"

#: Hard cap for the router body, in bytes.  Exceeding it truncates loudly.
ROUTER_BUDGET_BYTES = 6000

_PATHLIKE = Union[str, os.PathLike]


# --------------------------------------------------------------------------
# Named errors — a missing file is never a silent empty result.
# --------------------------------------------------------------------------


class NotesError(Exception):
    """Base class for note-vault failures."""


class InvalidNoteNameError(NotesError, ValueError):
    """The note name is not a safe single filename inside the vault."""


class NotesDirectoryError(NotesError, FileNotFoundError):
    """The notes directory does not exist."""


class NoteNotFoundError(NotesError, FileNotFoundError):
    """No note with that name exists in the notes directory."""


class NoteExistsError(NotesError, FileExistsError):
    """A note with that name already exists and overwrite was not requested."""


class RouterNotFoundError(NotesError, FileNotFoundError):
    """The always-loaded router file does not exist."""


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------


def resolve_vault_dir(vault_dir: Optional[_PATHLIKE] = None) -> Path:
    """Return the vault directory, expanding ``~``.

    Resolution order: explicit *vault_dir*, then ``$CLAIRE_VAULT_DIR``, then the
    ``vault_dir`` key in Claire's config file, then
    ``~/.local/share/claire/vault`` (the same data root Claire's SQLite database
    uses).  The path is returned even if it does not exist yet —
    ``create_note`` creates it on demand.

    An environment variable outranks the config file on purpose: it is the
    per-run override a test or a one-off ``CLAIRE_VAULT_DIR=... python ...``
    uses, and nothing on disk should be able to defeat it.
    """
    if vault_dir is not None:
        return Path(vault_dir).expanduser()
    env = os.environ.get(VAULT_ENV_VAR)
    if env:
        return Path(env).expanduser()
    configured = _configured_vault_dir()
    if configured is not None:
        return configured
    return Path.home() / ".local" / "share" / "claire" / VAULT_DIRNAME


def _config_file_path() -> Optional[Path]:
    """Where Claire's JSON config lives, or ``None`` if that cannot be told.

    ``claire.config`` is imported lazily: this module is on the reply hot path
    and its "dependency-free" promise should not become a heavy import at
    module load.  The environment variable and default path are the same pair
    ``claire.config`` itself uses, so the panel, the daemon and this module
    cannot disagree about which file is authoritative.
    """
    try:
        from claire.config import default_config_path
    except Exception:  # noqa: BLE001 - a missing config module is not fatal here
        return None
    try:
        override = os.environ.get("CLAIRE_CONFIG_PATH")
        return Path(override).expanduser() if override else Path(default_config_path())
    except Exception:  # noqa: BLE001
        return None


@lru_cache(maxsize=8)
def _configured_vault_dir_at(path_text: str, mtime_ns: int, size: int) -> Optional[str]:
    """Read ``vault_dir`` from *path_text*, cached on the file's identity.

    The ``(mtime_ns, size)`` cache key is why this is a stat — not a JSON parse —
    on every reply, and why editing the config file takes effect on the next
    call with no restart.  A malformed or unreadable config yields ``None``
    rather than raising: an unreadable config must not make memory unreadable.
    """
    try:
        with open(path_text, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    value = loaded.get("vault_dir")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _configured_vault_dir() -> Optional[Path]:
    """The ``vault_dir`` config key as an expanded path, or ``None`` for no opinion."""
    path = _config_file_path()
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    value = _configured_vault_dir_at(str(path), stat.st_mtime_ns, stat.st_size)
    if not value:
        return None
    try:
        return Path(value).expanduser()
    except Exception:  # noqa: BLE001 - an unusable path is "no opinion"
        return None


def resolve_notes_dir(
    notes_dir: Optional[_PATHLIKE] = None,
    vault_dir: Optional[_PATHLIKE] = None,
) -> Path:
    """Return the directory holding note files.

    ``notes_dir`` wins outright; otherwise it is ``<vault>/notes``.
    """
    if notes_dir is not None:
        return Path(notes_dir).expanduser()
    return resolve_vault_dir(vault_dir) / NOTES_DIRNAME


def resolve_router_path(vault_dir: Optional[_PATHLIKE] = None) -> Path:
    """Return the path of the always-loaded router file (``<vault>/CLAIRE.md``)."""
    return resolve_vault_dir(vault_dir) / ROUTER_FILENAME


# --------------------------------------------------------------------------
# Frontmatter (tiny YAML subset — no PyYAML, no new dependencies)
# --------------------------------------------------------------------------

_FENCE = "---"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Split *text* into ``(frontmatter dict, body)``.

    Only the flat ``key: value`` subset is understood, which is exactly what
    Claire writes.  A leading block that does not open and close with ``---``
    is treated as ordinary body text (no frontmatter).  Keys are lowercased.
    """
    if not text:
        return {}, ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return {}, text
    meta: Dict[str, str] = {}
    for index in range(1, len(lines)):
        raw = lines[index]
        if raw.strip() == _FENCE:
            body = "\n".join(lines[index + 1 :])
            if text.endswith("\n") and body:
                body += "\n"
            return meta, body
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        if key:
            meta[key] = _unquote(value)
    # Unterminated block: not frontmatter after all.
    return {}, text


def strip_frontmatter(text: str) -> str:
    """Return *text* without a leading frontmatter block."""
    return parse_frontmatter(text)[1]


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")


def _first_heading(text: str) -> Optional[str]:
    for raw in text.splitlines():
        match = _HEADING_RE.match(raw)
        if match:
            return match.group("title").strip()
    return None


def _humanise(stem: str) -> str:
    """Turn ``dentist-appointment`` into ``Dentist appointment`` as a last resort."""
    cleaned = re.sub(r"[_\-]+", " ", stem).strip()
    if not cleaned:
        return stem
    return cleaned[0].upper() + cleaned[1:]


def extract_title(text: str, fallback: str = "") -> str:
    """Title for a note: frontmatter ``title``, else first heading, else *fallback*.

    The fallback is humanised so a title is never an empty string.
    """
    meta, body = parse_frontmatter(text)
    title = (meta.get("title") or "").strip()
    if title:
        return title
    heading = _first_heading(body) or _first_heading(text)
    if heading:
        return heading
    return _humanise(fallback) if fallback else ""


# --------------------------------------------------------------------------
# Names and paths — traversal is rejected explicitly
# --------------------------------------------------------------------------

_UNSAFE_CHARS = ("/", "\\", "\x00", "\n", "\r", "\t")
_MAX_NAME_LEN = 120


def normalize_note_name(name: str) -> str:
    """Validate a note *name* and return its bare stem (no ``.md``).

    Accepts ``"dentist"`` and ``"dentist.md"``.  Raises
    :class:`InvalidNoteNameError` — naming the offending value — for anything
    that is not one plain filename inside the vault: absolute paths, ``~``
    paths, separators, ``..`` traversal, NUL/newlines, hidden names, or names
    that are over-long.
    """
    if not isinstance(name, str):
        raise InvalidNoteNameError(
            f"Invalid note name {name!r}: expected a string, got {type(name).__name__}"
        )
    raw = name.strip()
    if raw.lower().endswith(".md"):
        raw = raw[: -len(".md")].strip()
    if not raw:
        raise InvalidNoteNameError(
            f"Invalid note name {name!r}: the name is empty (a note is one "
            f"filename such as 'dentist')"
        )
    if raw in (".", "..") or ".." in raw:
        raise InvalidNoteNameError(
            f"Invalid note name {name!r}: '..' path traversal is not allowed; "
            f"a note name is a single filename inside the notes directory"
        )
    for char in _UNSAFE_CHARS:
        if char in raw:
            pretty = char.encode("unicode_escape").decode("ascii")
            raise InvalidNoteNameError(
                f"Invalid note name {name!r}: contains {pretty!r}; a note name "
                f"is a single filename, not a path"
            )
    if Path(raw).is_absolute() or raw.startswith("~"):
        raise InvalidNoteNameError(
            f"Invalid note name {name!r}: absolute and ~ paths are not allowed; "
            f"notes live inside the notes directory"
        )
    if raw.startswith("."):
        raise InvalidNoteNameError(
            f"Invalid note name {name!r}: hidden files are not used as notes"
        )
    if len(raw) > _MAX_NAME_LEN:
        raise InvalidNoteNameError(
            f"Invalid note name {name!r}: {len(raw)} characters exceeds the "
            f"{_MAX_NAME_LEN}-character limit"
        )
    return raw


def _note_path(name: str, notes_dir: Path) -> Path:
    """Resolve ``<notes_dir>/<stem>.md``, re-checking the containment.

    Belt and braces: :func:`normalize_note_name` has already rejected
    traversal, and this second check refuses anything that resolves outside
    the notes directory anyway.
    """
    stem = normalize_note_name(name)
    base = notes_dir.expanduser()
    candidate = (base / f"{stem}.md").resolve()
    base_resolved = base.resolve()
    if candidate.parent != base_resolved:
        raise InvalidNoteNameError(
            f"Invalid note name {name!r}: resolves to {candidate}, outside the "
            f"notes directory {base_resolved}"
        )
    return candidate


def _require_notes_dir(notes_dir: Path) -> Path:
    """Return *notes_dir* if it is a directory, else raise a named error."""
    if not notes_dir.exists():
        raise NotesDirectoryError(
            f"Notes directory not found: {notes_dir}. Nothing to list, read or "
            f"search. Create it with create_note('<name>', '<body>') or set "
            f"${VAULT_ENV_VAR}."
        )
    if not notes_dir.is_dir():
        raise NotesDirectoryError(
            f"Notes directory is not a directory: {notes_dir}"
        )
    return notes_dir


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NoteInfo:
    """One note file as seen from the outside: name, path, size, title."""

    name: str
    path: Path
    size_bytes: int
    title: str


@dataclass(frozen=True)
class Note:
    """A note file's full content plus its metadata."""

    name: str
    path: Path
    title: str
    text: str
    size_bytes: int
    frontmatter: Dict[str, str] = field(default_factory=dict)

    @property
    def body(self) -> str:
        """The note text with frontmatter removed."""
        return strip_frontmatter(self.text)


@dataclass(frozen=True)
class NoteMatch:
    """A ranked search hit: which note matched, on which line, and how well."""

    name: str
    path: Path
    title: str
    line_no: int
    line: str
    score: float
    matched_in: str  # "title" | "body"


# --------------------------------------------------------------------------
# Listing / reading
# --------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _iter_note_files(notes_dir: Path) -> List[Path]:
    """All ``*.md`` files directly inside *notes_dir*, sorted by name.

    Sorting is by lowercased filename so behaviour is stable across
    filesystems that order differently.  Subdirectories are not descended
    into: a note name is one filename.

    A symlink that points outside the notes directory is skipped, so listing
    agrees with :func:`read_note` (which refuses such a path) and a listing
    can never surface content from outside the vault.
    """
    base = notes_dir.resolve()
    files: List[Path] = []
    for entry in notes_dir.iterdir():
        if not entry.is_file() or entry.suffix.lower() != ".md":
            continue
        try:
            if entry.resolve().parent != base:
                continue
        except OSError:  # pragma: no cover - broken/looping link
            continue
        files.append(entry)
    return sorted(files, key=lambda p: (p.name.lower(), p.name))


def _info_for(path: Path) -> NoteInfo:
    text = _read_text(path)
    return NoteInfo(
        name=path.stem,
        path=path,
        size_bytes=path.stat().st_size,
        title=extract_title(text, fallback=path.stem),
    )


def list_notes(
    notes_dir: Optional[_PATHLIKE] = None,
    vault_dir: Optional[_PATHLIKE] = None,
) -> List[NoteInfo]:
    """List the note files in the vault, with name, path, byte size and title.

    Raises :class:`NotesDirectoryError` — naming the directory — when the
    notes directory is absent, rather than returning an empty list that would
    look like "no notes".
    """
    resolved = _require_notes_dir(resolve_notes_dir(notes_dir, vault_dir))
    return [_info_for(path) for path in _iter_note_files(resolved)]


def read_note(
    name: str,
    notes_dir: Optional[_PATHLIKE] = None,
    vault_dir: Optional[_PATHLIKE] = None,
) -> Note:
    """Read one note by *name*.

    Raises :class:`NoteNotFoundError` naming both the note and the directory
    it was looked for in, and :class:`InvalidNoteNameError` for an unsafe
    name.
    """
    resolved = resolve_notes_dir(notes_dir, vault_dir)
    path = _note_path(name, resolved)
    if not path.exists():
        raise NoteNotFoundError(
            f"Note not found: {normalize_note_name(name)!r} in notes directory "
            f"{resolved} (looked for {path}). Use list_notes() to see what is "
            f"there, or create_note() to add it."
        )
    if not path.is_file():
        raise NoteNotFoundError(
            f"Note not found: {normalize_note_name(name)!r} in notes directory "
            f"{resolved} — {path} exists but is not a file"
        )
    text = _read_text(path)
    meta, _ = parse_frontmatter(text)
    return Note(
        name=path.stem,
        path=path,
        title=extract_title(text, fallback=path.stem),
        text=text,
        size_bytes=path.stat().st_size,
        frontmatter=meta,
    )


# --------------------------------------------------------------------------
# Cheap search — ranked hits with the matching line
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def _tokens(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _title_line(text: str, title: str) -> Tuple[int, str]:
    """Find the 1-based line that carries the note's *title*.

    Prefers the first heading, then a ``title:`` frontmatter line, then the
    first non-empty line.  Falls back to ``(1, title)`` for an empty file.
    """
    lines = text.splitlines()
    for index, raw in enumerate(lines, start=1):
        match = _HEADING_RE.match(raw)
        if match and match.group("title").strip() == title:
            return index, raw.strip()
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.lower().startswith("title:") and ":" in stripped:
            if _unquote(stripped.split(":", 1)[1]).strip() == title:
                return index, stripped
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped and stripped != _FENCE:
            return index, stripped
    return 1, title


def search_notes(
    query: str,
    notes_dir: Optional[_PATHLIKE] = None,
    limit: int = 10,
    vault_dir: Optional[_PATHLIKE] = None,
) -> List[NoteMatch]:
    """Search note titles and bodies; return ranked matches with the line.

    Ranking is deliberately cheap and deterministic (no embeddings, no new
    dependencies): the query is lowercased and split into word tokens; a line
    scores one point per distinct token it contains, title matches score four
    points each, and a whole-query substring hit scores five.  Ties break by
    note name and then by line number, so results are stable.

    Returns at most *limit* matches, best first.  Raises ``ValueError`` for an
    empty query and :class:`NotesDirectoryError` when the vault has no notes
    directory.
    """
    tokens = _tokens(query)
    if not tokens:
        raise ValueError(
            "search_notes() requires a non-empty query; got "
            f"{query!r}. An empty query would match everything, so it is "
            f"refused rather than silently returning nothing."
        )
    needle = (query or "").strip().lower()
    distinct = sorted(set(tokens))

    resolved = _require_notes_dir(resolve_notes_dir(notes_dir, vault_dir))
    matches: List[NoteMatch] = []
    for path in _iter_note_files(resolved):
        text = _read_text(path)
        title = extract_title(text, fallback=path.stem)
        title_lower = title.lower()
        title_score = 4.0 * sum(1 for token in distinct if token in title_lower)
        if needle and needle in title_lower:
            title_score += 5.0
        title_no, title_text = _title_line(text, title)

        best_score, best_index, best_raw = 0.0, 0, ""
        for index, raw in enumerate(text.splitlines(), start=1):
            lowered = raw.lower()
            line_score = sum(1.0 for token in distinct if token in lowered)
            if not line_score:
                continue
            if needle and needle in lowered:
                line_score += 5.0
            if index == title_no:
                line_score += 2.0
            # Earlier lines win ties: subtract a negligible position penalty.
            ranked = line_score - (index * 1e-4)
            if ranked > best_score:
                best_score, best_index, best_raw = ranked, index, raw.strip()

        total = title_score + best_score
        if total <= 0:
            continue
        if best_index == 0:
            line_no, line, matched_in = title_no, title_text, "title"
        elif best_index == title_no:
            line_no, line, matched_in = best_index, best_raw, "title"
        else:
            line_no, line, matched_in = best_index, best_raw, "body"
        matches.append(
            NoteMatch(
                name=path.stem,
                path=path,
                title=title,
                line_no=line_no,
                line=line,
                score=round(total, 3),
                matched_in=matched_in,
            )
        )

    matches.sort(key=lambda m: (-m.score, m.name.lower(), m.line_no))
    if limit is not None and limit >= 0:
        return matches[:limit]
    return matches


# --------------------------------------------------------------------------
# Router honesty — which notes does the router never mention?
# --------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _name_pattern(name: str) -> "re.Pattern[str]":
    """Compiled word-ish matcher for a note name.

    The boundaries still match ``[[name]]``, ``notes/name.md`` and ``name.md``,
    but not ``name`` inside ``nameserver``.  Cached because the vault can hold
    many notes and the router is re-checked repeatedly.
    """
    return re.compile(
        r"(?<![\w\-\.])" + re.escape(name.lower()) + r"(?:\.md)?(?![\w\-])",
        re.UNICODE,
    )


def _name_appears(name: str, router_lower: str) -> bool:
    return bool(_name_pattern(name).search(router_lower))


def unreferenced_notes(
    router_text: Optional[str] = None,
    notes_dir: Optional[_PATHLIKE] = None,
    vault_dir: Optional[_PATHLIKE] = None,
    router_path: Optional[_PATHLIKE] = None,
) -> List[NoteInfo]:
    """Return notes that *router_text* never mentions.

    Side-effect free: it reads the vault and, when *router_text* is ``None``,
    the router file at *router_path* (default ``<vault>/CLAIRE.md``).  A note
    counts as referenced when its bare name (or ``name.md``) appears as a word
    in the router body, so ``[[dentist]]``, ``notes/dentist.md`` and plain
    ``dentist`` all count.

    The router body is used, not its frontmatter, so a title that happens to
    equal a note name only counts if the pointer is in the body.

    Raises :class:`RouterNotFoundError` naming the path when the router file
    is absent, and :class:`NotesDirectoryError` when the notes directory is
    absent — never a silent empty result.
    """
    resolved_notes = _require_notes_dir(resolve_notes_dir(notes_dir, vault_dir))
    if router_text is None:
        path = (
            Path(router_path).expanduser()
            if router_path is not None
            else resolve_router_path(vault_dir)
        )
        if not path.exists():
            raise RouterNotFoundError(
                f"Router file not found: {path}. Cannot report unreferenced "
                f"notes without the router body; pass router_text=... to check "
                f"a body directly."
            )
        router_text = _read_text(path)
    _, body = parse_frontmatter(router_text or "")
    router_lower = body.lower()
    return [
        info
        for info in (_info_for(p) for p in _iter_note_files(resolved_notes))
        if not _name_appears(info.name, router_lower)
    ]


# --------------------------------------------------------------------------
# Creating notes
# --------------------------------------------------------------------------


def _frontmatter_scalar(value: str) -> str:
    value = str(value)
    needs_quotes = (
        value == ""
        or value != value.strip()
        or any(char in value for char in (":", "#", '"', "'"))
    )
    if needs_quotes:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def render_frontmatter(meta: Dict[str, object]) -> str:
    """Render a flat frontmatter block (used by :func:`create_note`)."""
    lines = [_FENCE]
    for key, value in meta.items():
        if value is None:
            continue
        lines.append(f"{key}: {_frontmatter_scalar(str(value))}")
    lines.append(_FENCE)
    return "\n".join(lines) + "\n"


def create_note(
    name: str,
    body: str = "",
    *,
    title: Optional[str] = None,
    overwrite: bool = False,
    notes_dir: Optional[_PATHLIKE] = None,
    vault_dir: Optional[_PATHLIKE] = None,
    extra_frontmatter: Optional[Dict[str, object]] = None,
    today: Optional[str] = None,
) -> NoteInfo:
    """Create a markdown note with frontmatter; return its :class:`NoteInfo`.

    Refuses to overwrite an existing note unless *overwrite* is true
    (:class:`NoteExistsError` names the note and the path), and refuses any
    name that would escape the vault (:class:`InvalidNoteNameError`) — so
    ``create_note('../../etc/passwd', ...)`` writes nothing anywhere.

    The notes directory is created on demand.  If *body* already begins with
    its own ``---`` frontmatter block it is written verbatim; otherwise a
    frontmatter block with ``title`` and ``created`` is generated.
    """
    resolved = resolve_notes_dir(notes_dir, vault_dir)
    path = _note_path(name, resolved)
    stem = path.stem

    if path.exists() and not overwrite:
        raise NoteExistsError(
            f"Note already exists: {stem!r} at {path}. Pass overwrite=True to "
            f"replace it, or choose another name."
        )

    resolved.mkdir(parents=True, exist_ok=True)

    text_body = "" if body is None else str(body)
    if text_body.lstrip().startswith(_FENCE):
        document = text_body
    else:
        meta: Dict[str, object] = {"title": title or _humanise(stem)}
        meta["created"] = today or _datetime.date.today().isoformat()
        for key, value in (extra_frontmatter or {}).items():
            meta[str(key).lower()] = value
        document = render_frontmatter(meta)
        if text_body:
            separator = "" if text_body.startswith("\n") else "\n"
            document += separator + text_body
        if not document.endswith("\n"):
            document += "\n"

    mode = "w" if overwrite else "x"
    try:
        with open(path, mode, encoding="utf-8", newline="\n") as handle:
            handle.write(document)
    except FileExistsError as exc:  # race: someone created it between the two checks
        raise NoteExistsError(
            f"Note already exists: {stem!r} at {path} (created concurrently). "
            f"Pass overwrite=True to replace it."
        ) from exc

    _restrict_to_owner(path)
    return _info_for(path)


def _restrict_to_owner(path: Path) -> None:
    """Make a vault file readable only by its owner (mode 0600).

    The vault usually inherits a private umask, but "usually" is not a
    guarantee: memory is the owner's own facts, so the mode is set explicitly
    rather than assumed.  A filesystem that cannot chmod is not an error — the
    write already happened and the caller must still get its note.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - filesystem without chmod
        pass


def ensure_notes_dir(
    notes_dir: Optional[_PATHLIKE] = None,
    vault_dir: Optional[_PATHLIKE] = None,
) -> Path:
    """Create the notes directory (and the vault around it) if it is missing.

    Returns the resolved directory.  This is the one place that is allowed to
    create both directories, so "why does my vault have no ``notes/``?" has a
    single answer; the panel calls it before its first write, and a fresh
    install gets a well-formed vault instead of an error naming a directory
    that does not exist.
    """
    resolved = resolve_notes_dir(notes_dir, vault_dir)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _write_note_document(path: Path, document: str) -> None:
    """Replace *path*'s text atomically, at mode 0600.

    Written to a sibling temporary file and ``os.replace``\\ d into place: a
    crash mid-write leaves the previous note intact rather than a half-written
    one, which matters because the file is read back as memory.
    """
    directory = path.parent
    handle_fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=str(directory)
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(document)
        _restrict_to_owner(Path(temp_name))
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:  # pragma: no cover - already gone
            pass
        raise
    _restrict_to_owner(path)


def update_note(
    name: str,
    body: str = "",
    *,
    title: Optional[str] = None,
    notes_dir: Optional[_PATHLIKE] = None,
    vault_dir: Optional[_PATHLIKE] = None,
    extra_frontmatter: Optional[Dict[str, object]] = None,
    today: Optional[str] = None,
) -> NoteInfo:
    """Replace an existing note's body, preserving its identity in frontmatter.

    :func:`create_note` writes a *new* note; this is its counterpart for an
    edit, and it is the single path the panel uses so the browser and Claire's
    own parser cannot disagree about the format.

    Existing frontmatter keys survive (``created``, ``tags`` and anything else
    the owner or a previous version wrote); ``title`` is replaced when given and
    kept otherwise; ``updated`` is always stamped.  Raise
    :class:`NoteNotFoundError` for a note that does not exist — an edit is never
    a silent create, because that is how a typo turns into a second note.

    If *body* begins with its own ``---`` frontmatter block it is written
    verbatim, exactly as :func:`create_note` does.
    """
    resolved = _require_notes_dir(resolve_notes_dir(notes_dir, vault_dir))
    path = _note_path(name, resolved)
    if not path.exists() or not path.is_file():
        raise NoteNotFoundError(
            f"Note not found: {path.stem!r} in notes directory {resolved} "
            f"(looked for {path}). Use create_note() to add it, or list_notes() "
            f"to see what is there."
        )

    existing_text = _read_text(path)
    existing_meta, _ = parse_frontmatter(existing_text)
    text_body = "" if body is None else str(body)

    if text_body.lstrip().startswith(_FENCE):
        document = text_body
    else:
        meta: Dict[str, object] = {str(key): value for key, value in existing_meta.items()}
        meta["title"] = title or str(existing_meta.get("title") or _humanise(path.stem))
        meta.setdefault("created", today or _datetime.date.today().isoformat())
        meta["updated"] = today or _datetime.date.today().isoformat()
        for key, value in (extra_frontmatter or {}).items():
            meta[str(key).lower()] = value
        document = render_frontmatter(meta)
        if text_body:
            separator = "" if text_body.startswith("\n") else "\n"
            document += separator + text_body
        if not document.endswith("\n"):
            document += "\n"

    _write_note_document(path, document)
    return _info_for(path)


def delete_note(
    name: str,
    *,
    notes_dir: Optional[_PATHLIKE] = None,
    vault_dir: Optional[_PATHLIKE] = None,
) -> NoteInfo:
    """Delete one note and return what was removed.

    Raises :class:`NoteNotFoundError` when there is no such note, so a delete
    that removes nothing is never reported as success.  There is no undo and no
    trash directory: the file is the memory, and the owner asked for it gone.
    """
    resolved = _require_notes_dir(resolve_notes_dir(notes_dir, vault_dir))
    path = _note_path(name, resolved)
    if not path.exists() or not path.is_file():
        raise NoteNotFoundError(
            f"Note not found: {path.stem!r} in notes directory {resolved} "
            f"(looked for {path}). Nothing was deleted."
        )
    info = _info_for(path)
    path.unlink()
    return info

