# Memory Router

The always-loaded router document: a row in PostgreSQL with pgvector (`memory_documents`, `doc_kind = 'router'`) on the default backend, or `<vault>/CLAIRE.md` when the backend is explicitly set to `vault`, plus the rules that keep it from rotting.

A router, not a database. It answers *"what do I know about, and where is it?"* — one line per
fact, plus pointers to the notes. Anything bigger than a line belongs in a note, and only the
pointer stays here. The router and the notes are rows in PostgreSQL with pgvector
([`pg.py`](pg.py), runbook in [`docs/PGVECTOR.md`](../../docs/PGVECTOR.md)); the file form
(`<vault>/CLAIRE.md` beside `notes/*.md`) is the retired `vault` backend, reachable only by
setting `memory_backend` to `vault`. See [MEMORY.md](../../docs/MEMORY.md) for the owner's guide
and [`notes.py`](notes.py) for that legacy path's note files.

## Scope

- File: `src/claire/memory/router.py`.
- Tests: `tests/test_memory_router.py`.
- Owns: reading, byte-accounting, truncating and rendering the router document (`CLAIRE.md` in
  `vault` mode, the `memory_documents` row in `postgres` mode); creating the file from the
  built-in template when it is missing. `postgres` mode never invents a router: a missing row
  raises `RouterNotFoundError` with the fix in the message, not a template.
- Shared rendering: `PostgresMemoryRouter` ([`pg_router.py`](pg_router.py)) is the reader the
  default backend uses. It reads the router row through `pg.get_router()` and inherits
  `render_for_prompt()`, the byte accounting and the over-budget warning below; it overrides only
  the header (the panel's Memory page, not a file) and the read source. The row carries the same
  parsed `frontmatter`, so the `budget_bytes` override resolves identically on both backends.
- Does **not** own: path resolution or frontmatter parsing — both come from
  [`notes.py`](notes.py) (`resolve_router_path()`, `parse_frontmatter()`,
  `ROUTER_BUDGET_BYTES`, `RouterNotFoundError`). No path logic is duplicated here.
- Depends on nothing outside the standard library and the sibling module. **No PyYAML.**

## The document format

```markdown
---
title: Claire memory router
role: router
always_loaded: true
budget_bytes: 6000
updated: 2026-01-31
---

# Pointers

- dentist: notes/dentist.md — Dr. Ruiz, Wednesdays at 16:00.
- car: notes/car-service.md — garage, mileage and next service.
```

Frontmatter is the flat `key: value` subset only, between `---` fences, lowercased keys.
`budget_bytes` is an override; anything unusable (absent, non-numeric, `0`, negative) falls back
to `ROUTER_BUDGET_BYTES` (`6000`). A missing, unterminated or malformed block is not an error:
defaults apply and, for an unterminated block, the whole text is treated as body — nothing is
silently dropped.

On the Postgres path the same two halves are columns: `frontmatter` as `jsonb` (what
`PostgresMemoryRouter.reload()` puts back on the object) plus `frontmatter_raw` as the original
bytes, so a one-way export round-trips byte-for-byte, and `body` holds the Markdown above.
`migrate_md_to_pg.py` runs `notes.parse_frontmatter()` on the way in; the panel's memory editor
hands the stored frontmatter straight back.

## API

| Name | Contract |
| --- | --- |
| `MemoryRouter` | Dataclass with `path`, `frontmatter`, `body`, `created_from_template`, and the derived `budget_bytes`, `size_bytes`, `over_budget`, `dropped_bytes`. |
| `MemoryRouter.reload()` | Re-read the file from disk now, replacing `frontmatter` and `body`. Returns `self`. |
| `MemoryRouter.render_for_prompt()` | Re-read from disk and return the prompt text. **Never cached.** |
| `load_router(path=None, *, vault_dir=None, create=True)` | Read the router. `create=True` (default) builds a missing file from the template; `create=False` raises `RouterNotFoundError`. |
| `ensure_router(path=None, *, vault_dir=None)` | Load, creating from the template if absent. Idempotent; never overwrites an existing file, including one the owner edited. |
| `router_template(updated=None)` | The built-in starter router (one-line facts plus pointers), well under budget. |
| `MEMORY_HEADER` | The short header rendered above the body. |

### Byte accounting

`size_bytes` is the real UTF-8 byte length of the **body** (what the prompt pays for; frontmatter
is metadata and is not rendered). `budget_bytes` is the effective cap. `over_budget` is
`size_bytes > budget_bytes`. `dropped_bytes` is what truncation drops for the current body, and is
`0` within budget. Rendering never mutates `body`, so `over_budget`/`dropped_bytes` keep describing
the file even after a render — the warning stays true on every subsequent call.

### `render_for_prompt()`

Within budget, the body is returned **intact** under a short header saying this is Claire's
always-loaded memory, re-read from disk, that the owner can edit — with no warning and no marker
that could be confused with content.

Over budget, two things happen:

1. The body is **truncated at a line boundary** — a line is kept whole or not at all, so the model
   never sees half a fact. A single first line larger than the whole budget means nothing is kept;
   the warning still says so.
2. A **loud warning is prepended** (before the header and body), naming the real size, the budget
   and the exact number of bytes dropped. The wording describes the router itself, never a file or
   a directory, so the same notice holds whichever backend stores the document:

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! WARNING: YOUR ALWAYS-LOADED MEMORY ROUTER IS OVER BUDGET AND WAS TRUNCATED
!! Real size: 8214 bytes. Budget: 6000 bytes. Dropped: 2276 bytes.
!! Those dropped bytes are NOT being loaded: the end of the router is invisible to me.
!! Fix it by shortening the router and keeping detail in separate notes, which are searched on demand instead of always loaded.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

This is deliberate and is the point of the cap: rot becomes visible and self-correcting instead of
silent. A router that quietly grew to 40 KB would degrade answers for weeks before anyone noticed.

### Re-read on every request — no caching

`render_for_prompt()` re-reads the router on **every** call. Nothing is memoised, not even for
the duration of one object's life: the owner edits `CLAIRE.md` or the router row, and the next
request already sees it. There is no restart, no reload endpoint and no cache to invalidate.
Editing the router is the whole interface.

The rendered text is put into the **system prompt** each turn, never appended to conversation
history. That is what makes it cost zero history tokens: re-rendering is flat, whereas appending
the file as a chat message every turn would grow the history without bound and rot the context.

## Missing, unreadable and undecodable files

Every failure is NAMED; no code path returns `''` or an empty body that looks like "no memory".

| Situation | Result |
| --- | --- |
| File missing, `ensure_router()` / `load_router()` | Created from `router_template()`. Reported twice: `created_from_template=True` on the returned object, and an INFO log line on `claire.memory.router` naming the path and size. |
| File missing, `load_router(..., create=False)` | `RouterNotFoundError` (from `notes.py`) naming the path. |
| Path exists but is not a regular file (e.g. a directory) | `RouterReadError` naming the path. |
| Permission denied / any other `OSError` while reading | `RouterReadError` naming the path and the OS reason. |
| Body is not valid UTF-8 | `RouterDecodeError` naming the path, the byte offset and the reason. Never decoded with `errors="replace"` — a lossy decode would silently drop memory. |
| File deleted after loading, then `render_for_prompt()` | `RouterNotFoundError`. |
| Parent directory cannot be created / template cannot be written | `RouterWriteError`; nothing is reported as created. |

`RouterError` derives from `NotesError`, and `RouterReadError`/`RouterWriteError` also derive from
`OSError`, so `except OSError` at a call site behaves as expected.

The `postgres` path names its failures the same way. `PostgresMemoryRouter.reload()` raises
`RouterNotFoundError` when `memory_documents` holds no row with `doc_kind = 'router'`, quoting the
row it wanted and the two ways to make it (run `python -m claire.memory.migrate_md_to_pg`, or
create it in the panel's memory editor). `system_prompt.py` catches whatever the store raises and
prints `MEMORY_UNAVAILABLE_MARKER` with the exception name and reason, or no memory section at all
when the router was never being forced; a database outage is a visible marker, never a crash and
never an empty string that reads as "no memory".

## Creation is idempotent

`ensure_router()` writes the template only when the file is absent. It never overwrites an existing
router, so an owner-edited file survives every subsequent call untouched, and the second call
reports `created_from_template=False`. If the file appears between the existence check and the
write (a race with the owner), the owner's copy wins on the next load; the template is only ever
written into a path that did not exist.

## Template

`router_template()` is genuine starter memory, not a placeholder: a handful of one-line facts about
the owner (name, timezone, answer style, standing preferences) plus the `# Pointers` section and a
short note on keeping the file honest. It is well under a quarter of the budget, so a fresh
router starts green.

## Non-goals

- No LLM calls, no embeddings, no search: `router.py` reads the router whole and renders it whole.
  Embedding belongs to the store, which embeds chunks as they are written (`pg.py`), not to this
  module and not to the prompt path.
- No background writes to the router. In `vault` mode only `ensure_router()`/`load_router(create=True)`
  write, and only when the file does not exist. In `postgres` mode the row is written by the panel's
  memory editor and by the one-shot importer, never by the prompt path.
- No trimming, rewriting or "helpful" tidying of what the owner wrote — over-budget content is
  reported, not silently reorganised. Moving detail into notes stays a human decision.
- No new dependency (no PyYAML, no markdown parser).
