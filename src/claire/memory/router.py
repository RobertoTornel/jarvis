"""The always-loaded memory router — ``<vault>/CLAIRE.md``.

Claire keeps one small memory file that is read on **every** request.  It is a
**router, not a database**: a fact that is bigger than one line belongs in a
note file under ``notes/``, and only a *pointer* to it stays here.

Three properties make that safe:

* **Re-read from disk on every call.**  ``render_for_prompt()`` never caches:
  correcting the file corrects Claire's behaviour on the very next thing you
  say, with no restart.  The content is re-rendered into the system prompt each
  turn rather than appended to the conversation history, so it costs **zero
  history tokens** and cannot grow the context without bound.
* **Hard-capped at** ``ROUTER_BUDGET_BYTES`` (6000) **bytes.**  Past the cap the
  body is truncated at a line boundary and a loud warning naming the real size
  is prepended, so rot is visible and self-correcting instead of silent.
* **Named errors, never silence.**  A missing, unreadable or undecodable file
  raises a named error; creating the file from the built-in template is always
  reported (``MemoryRouter.created_from_template`` plus an INFO log line).

Path logic is **not** duplicated here: ``resolve_router_path()`` and the
``key: value`` frontmatter subset come from the sibling
:mod:`claire.memory.notes` module.  See ``router.spec.md`` and ``docs/MEMORY.md``.
"""

from __future__ import annotations

import datetime as _datetime
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from .notes import (
    ROUTER_BUDGET_BYTES,
    NotesError,
    RouterNotFoundError,
    parse_frontmatter,
    resolve_router_path,
)

__all__ = [
    "MEMORY_HEADER",
    "MemoryRouter",
    "ROUTER_BUDGET_BYTES",
    "RouterDecodeError",
    "RouterError",
    "RouterReadError",
    "RouterWriteError",
    "ensure_router",
    "load_router",
    "router_template",
]

_LOGGER = logging.getLogger(__name__)

_PATHLIKE = Union[str, os.PathLike]

# --------------------------------------------------------------------------
# Named errors — a memory file is never silently empty.
# --------------------------------------------------------------------------

class RouterError(NotesError):
    """Base class for always-loaded router failures."""

class RouterReadError(RouterError, OSError):
    """The router file exists but could not be read or decoded."""

class RouterDecodeError(RouterReadError):
    """The router file's bytes are not valid UTF-8."""

class RouterWriteError(RouterError, OSError):
    """The router file could not be created on disk."""

# --------------------------------------------------------------------------
# The header that frames the body inside the system prompt
# --------------------------------------------------------------------------

#: Short, honest header: it tells the model where this text came from and tells
#: the owner that editing the file is enough (no restart, no code change).
MEMORY_HEADER = (
    "# Claire's always-loaded memory\n"
    "My standing memory of you, re-read from disk on every request: edit CLAIRE.md "
    "and my behaviour changes immediately, with no restart. One line per fact; "
    "anything longer belongs in notes/, with only a pointer here."
)

#: Rule drawn before and after the over-budget warning, so it cannot be missed
#: or mistaken for ordinary memory content.
_BUDGET_BANNER = "!" * 72

def _budget_warning(*, size_bytes: int, budget: int, dropped_bytes: int) -> str:
    """The loud, unmissable notice prepended to truncated memory.

    It names the **real** size, the budget and the number of bytes dropped, so
    the owner can see exactly how much memory is no longer reaching Claire.
    """
    return (
        f"{_BUDGET_BANNER}\n"
        "!! WARNING: YOUR ALWAYS-LOADED MEMORY (CLAIRE.md) IS OVER BUDGET AND WAS TRUNCATED\n"
        f"!! Real size: {size_bytes} bytes. Budget: {budget} bytes. "
        f"Dropped: {dropped_bytes} bytes.\n"
        "!! Those dropped bytes are NOT being loaded: the end of the file is invisible to me.\n"
        "!! Fix it by moving detail into notes/ and keeping only one-line pointers here.\n"
        f"{_BUDGET_BANNER}"
    )

# --------------------------------------------------------------------------
# The template a missing router is created from
# --------------------------------------------------------------------------

TEMPLATE_TITLE = "Claire's memory router"

def router_template(updated: Optional[str] = None) -> str:
    """Return the built-in router template, as genuine starter memory.

    A few one-line facts about the owner plus pointers to notes — deliberately
    far below the byte budget, and a valid router the owner can edit straight
    away.  *updated* defaults to today's date.
    """
    stamp = updated or _datetime.date.today().isoformat()
    return (
        "---\n"
        f"title: {TEMPLATE_TITLE}\n"
        "role: router\n"
        "always_loaded: true\n"
        f"budget_bytes: {ROUTER_BUDGET_BYTES}\n"
        f"updated: {stamp}\n"
        "---\n"
        "\n"
        "# About you\n"
        "\n"
        "- name: the owner of this machine; use it when you speak to them.\n"
        "- timezone: local time on this machine.\n"
        "- style: short, spoken answers first; detail only when asked.\n"
        "\n"
        "# Standing preferences\n"
        "\n"
        "- mornings: weather, calendar and anything urgent, five lines at most.\n"
        "- reminders: confirm the time out loud before saving one.\n"
        "\n"
        "# Pointers (the detail lives in notes/)\n"
        "\n"
        "- dentist: notes/dentist.md - Dr. Ruiz, Wednesdays at 16:00.\n"
        "- car: notes/car-service.md - garage, mileage and next service.\n"
        "\n"
        "# Keeping this file honest\n"
        "\n"
        "One line per fact. Anything bigger than a line goes into a note under\n"
        "`notes/`, and only the pointer stays here. This file is hard-capped at\n"
        f"{ROUTER_BUDGET_BYTES} bytes: past that it is truncated at a line boundary and\n"
        "the warning you would see at the top of this memory says so out loud.\n"
    )

# --------------------------------------------------------------------------
# Reading and byte accounting
# --------------------------------------------------------------------------

def _resolve_path(
    path: Optional[_PATHLIKE], vault_dir: Optional[_PATHLIKE]
) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return resolve_router_path(vault_dir)

def _read_source(path: Path) -> str:
    """Read *path* strictly: unreadable or undecodable bytes are named errors."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RouterReadError(
            f"Claire's memory router could not be read: {path} "
            f"({exc.strerror or exc}). It must be a readable UTF-8 markdown file."
        ) from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RouterDecodeError(
            f"Claire's memory router is not valid UTF-8: {path} "
            f"(byte {exc.start}: {exc.reason}). Refusing to guess, because a "
            "lossy decode would silently drop memory."
        ) from exc

def _coerce_budget(raw: object) -> Optional[int]:
    """Parse a frontmatter ``budget_bytes``; anything unusable means 'default'."""
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None

def _truncate_body(body: str, budget: int) -> Tuple[str, int]:
    """Cut *body* to whole lines fitting in *budget* bytes.

    Returns ``(kept_text, dropped_bytes)``.  Truncation is always at a line
    boundary: a line is kept whole or not at all, so the model never sees half
    a fact.  A first line bigger than the budget means nothing is kept.
    """
    data = body.encode("utf-8")
    if len(data) <= budget:
        return body, 0
    kept: "list[str]" = []
    used = 0
    for line in body.splitlines(keepends=True):
        width = len(line.encode("utf-8"))
        if used + width > budget:
            break
        kept.append(line)
        used += width
    kept_text = "".join(kept)
    return kept_text, len(data) - len(kept_text.encode("utf-8"))

# --------------------------------------------------------------------------
# The router object
# --------------------------------------------------------------------------

@dataclass
class MemoryRouter:
    """One always-loaded router file.

    ``path``/``frontmatter``/``body`` mirror the file as last read.  Call
    :meth:`reload` (or :meth:`render_for_prompt`, which reloads) to pick up an
    on-disk edit — nothing here is cached by design.
    """

    path: Path
    frontmatter: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    #: True only on the call that created the file from the template.
    created_from_template: bool = False

    # -- derived, always computed from the current body -------------------

    @property
    def budget_bytes(self) -> int:
        """Frontmatter ``budget_bytes`` when sane, else ``ROUTER_BUDGET_BYTES``."""
        override = _coerce_budget(self.frontmatter.get("budget_bytes"))
        return override if override is not None else ROUTER_BUDGET_BYTES

    @property
    def size_bytes(self) -> int:
        """Real byte size of the body (what the prompt would pay for)."""
        return len(self.body.encode("utf-8"))

    @property
    def over_budget(self) -> bool:
        return self.size_bytes > self.budget_bytes

    @property
    def dropped_bytes(self) -> int:
        """Bytes truncation drops for the current body (0 when within budget)."""
        if not self.over_budget:
            return 0
        _kept, dropped = _truncate_body(self.body, self.budget_bytes)
        return dropped

    # -- reading ----------------------------------------------------------

    def reload(self) -> "MemoryRouter":
        """Re-read the file from disk now, replacing frontmatter and body."""
        if not self.path.exists():
            raise RouterNotFoundError(
                f"Claire's memory router is missing: {self.path}. "
                "Call ensure_router() to create it from the built-in template."
            )
        if not self.path.is_file():
            raise RouterReadError(
                f"Claire's memory router is not a regular file: {self.path}. "
                "Move it aside and call ensure_router() to start a fresh one."
            )
        text = _read_source(self.path)
        self.frontmatter, self.body = parse_frontmatter(text)
        return self

    def render_for_prompt(self) -> str:
        """Re-read the file from disk and render it for the system prompt.

        There is deliberately **no caching**: every call re-reads the file, so
        a correction on disk changes behaviour on the very next request with no
        restart.  Within budget the body is returned intact under a short
        header.  Over budget it is truncated at a line boundary (whole lines
        only, never half a fact) and the loud warning comes first, naming the
        real size, the budget and the dropped bytes.
        """
        self.reload()
        if self.over_budget:
            kept, dropped = _truncate_body(self.body, self.budget_bytes)
            warning = _budget_warning(
                size_bytes=self.size_bytes,
                budget=self.budget_bytes,
                dropped_bytes=dropped,
            )
            return f"{warning}\n\n{MEMORY_HEADER}\n\n{kept}"
        return f"{MEMORY_HEADER}\n\n{self.body}".rstrip("\n") + "\n"

# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def ensure_router(
    path: Optional[_PATHLIKE] = None,
    *,
    vault_dir: Optional[_PATHLIKE] = None,
) -> MemoryRouter:
    """Load the router, creating it from the template if it does not exist.

    Creation is never silent: the returned object has
    ``created_from_template=True`` and an INFO line is logged.  Idempotent —
    an existing file (including one the owner has edited) is never overwritten.
    """
    target = _resolve_path(path, vault_dir)
    if target.exists():
        return MemoryRouter(path=target).reload()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RouterWriteError(
            f"Could not create the directory for Claire's memory router: "
            f"{target.parent} ({exc.strerror or exc})."
        ) from exc
    text = router_template()
    try:
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise RouterWriteError(
            f"Could not create Claire's memory router at {target} "
            f"({exc.strerror or exc}). Nothing was written."
        ) from exc

    router = MemoryRouter(path=target).reload()
    router.created_from_template = True
    _LOGGER.info(
        "Claire's memory router was missing; created it from the built-in "
        "template: %s (%d bytes)",
        target,
        router.size_bytes,
    )
    return router

def load_router(
    path: Optional[_PATHLIKE] = None,
    *,
    vault_dir: Optional[_PATHLIKE] = None,
    create: bool = True,
) -> MemoryRouter:
    """Read the router file.

    With ``create=True`` (the default) a missing file is created from the
    template exactly as :func:`ensure_router` does.  With ``create=False`` a
    missing file raises :class:`claire.memory.notes.RouterNotFoundError`.
    """
    target = _resolve_path(path, vault_dir)
    if not target.exists():
        if create:
            return ensure_router(target)
        raise RouterNotFoundError(
            f"Claire's memory router is missing: {target}. "
            "Pass create=True to build it from the template."
        )
    return MemoryRouter(path=target).reload()
