"""A :class:`MemoryRouter` whose source of truth is Postgres, not a file.

Subclassing :class:`~claire.memory.router.MemoryRouter` rather than
reimplementing it is deliberate: ``budget_bytes``, ``size_bytes``,
``over_budget``, ``dropped_bytes`` and the over-budget warning in
``build_memory_section`` must stay **byte-identical** to the file-backed router.
Inheriting the rendering path is the only way to guarantee that; a parallel
implementation would drift the moment the file router changed.

Only :meth:`reload` differs — it reads the ``memory_documents`` router row
instead of ``<vault>/CLAIRE.md``. Because ``render_for_prompt()`` calls
``reload()``, an edit made in the panel is picked up on the next turn with no
restart, which is the property the file router also had.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import pg
from .router import MemoryRouter, RouterNotFoundError

__all__ = ["PostgresMemoryRouter", "load_router_from_postgres", "router_exists"]

#: A synthetic path. Nothing stats it — it exists so that error messages and
#: ``repr()`` have something meaningful to show for a router that has no file.
ROUTER_SENTINEL_PATH = Path("postgres:memory_documents/router")


class PostgresMemoryRouter(MemoryRouter):
    """The always-loaded router document, read from Postgres."""

    #: Postgres has no ``CLAIRE.md`` file, so the inherited header would tell the
    #: model to edit a file that does not exist. Same promise ("edit it and my
    #: behaviour changes on the next request"), correct location.
    memory_header = (
        "# Claire's always-loaded memory\n"
        "My standing memory of you, re-read from my memory store on every request: "
        "edit it on the control panel's Memory page and my behaviour changes "
        "immediately, with no restart. One line per fact; anything longer belongs "
        "in notes, with only a pointer here."
    )

    def __init__(self, *, dsn: Optional[str] = None, path: Optional[Path] = None) -> None:
        # memory_header must be passed explicitly: the dataclass __init__ assigns
        # an instance attribute for every field with a default, which would
        # otherwise shadow the class-level override above.
        super().__init__(path=path or ROUTER_SENTINEL_PATH, memory_header=self.memory_header)
        self._dsn = dsn
        self.reload()

    # -- the single behavioural difference from the file router ------------

    def reload(self) -> "PostgresMemoryRouter":
        """Re-read the router row now, replacing frontmatter and body."""
        document = pg.get_router(dsn=self._dsn)
        if document is None:
            raise RouterNotFoundError(
                "Claire's memory router is not in Postgres: no row in "
                "memory_documents with doc_kind='router'. Import it with "
                "`python -m claire.memory.migrate_md_to_pg`, or create it in the "
                "panel's memory editor."
            )
        self.frontmatter = dict(document.frontmatter or {})
        self.body = document.body
        return self

    def save(self) -> "PostgresMemoryRouter":
        """Write the current frontmatter/body back, so the panel can edit it."""
        pg.upsert_document(
            source_path="CLAIRE.md",
            doc_kind="router",
            name="CLAIRE",
            title=str(self.frontmatter.get("title") or "Claire's memory router"),
            body=self.body,
            frontmatter=dict(self.frontmatter),
            always_loaded=True,
            budget_bytes=self.budget_bytes,
            dsn=self._dsn,
        )
        return self


def router_exists(dsn: Optional[str] = None) -> bool:
    """Whether a router document is present. Never raises."""
    try:
        return pg.get_router(dsn=dsn) is not None
    except Exception:
        return False


def load_router_from_postgres(dsn: Optional[str] = None) -> PostgresMemoryRouter:
    """Load the router from Postgres, raising :class:`RouterNotFoundError` if absent."""
    return PostgresMemoryRouter(dsn=dsn)
