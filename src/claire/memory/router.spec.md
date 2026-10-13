# Memory Router

The vault backend's always-loaded memory file, `<vault>/CLAIRE.md`, and the rules that keep it from rotting.

A router, not a database. It answers *"what do I know about, and where is it?"* — one line per
fact, plus pointers into `notes/`. Anything bigger than a line belongs in a note, and only the
pointer stays here. See [MEMORY.md](../../docs/MEMORY.md) for the owner's guide and
[`notes.py`](notes.py) for the other half (the note files themselves).

## Scope

- File: `src/claire/memory/router.py`.
- Tests: `tests/test_memory_router.py`.
- Owns: reading, byte-accounting, truncating and rendering `CLAIRE.md`; creating it from the
  built-in template when it is missing.
- Shared rendering: `PostgresMemoryRouter` ([`pg_router.py`](pg_router.py)) reads the router
  document from the store and inherits `render_for_prompt()`, the byte accounting and the
  over-budget warning below; it overrides only the header and the read source.
- Does **not** own: path resolution or frontmatter parsing — both come from
  [`notes.py`](notes.py) (`resolve_router_path()`, `parse_frontmatter()`,
  `ROUTER_BUDGET_BYTES`, `RouterNotFoundError`). No path logic is duplicated here.
- Depends on nothing outside the standard library and the sibling module. **No PyYAML.**

## The file format

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

## Creation is idempotent

`ensure_router()` writes the template only when the file is absent. It never overwrites an existing
router, so an owner-edited file survives every subsequent call untouched, and the second call
reports `created_from_template=False`. If the file appears between the existence check and the
write (a race with the owner), the owner's copy wins on the next load; the template is only ever
written into a path that did not exist.

## Template

`router_template()` is genuine starter memory, not a placeholder: a handful of one-line facts about
the owner (name, timezone, answer style, standing preferences) plus the `# Pointers` section and a
short note on keeping the file honest. It is well under a quarter of the budget, so a fresh vault
starts green.

## Non-goals

- No LLM calls, no embeddings, no search: the router is read whole and rendered whole.
- No background writes to `CLAIRE.md`. Only `ensure_router()`/`load_router(create=True)` write, and
  only when the file does not exist.
- No trimming, rewriting or "helpful" tidying of what the owner wrote — over-budget content is
  reported, not silently reorganised. Moving detail into notes stays a human decision.
- No new dependency (no PyYAML, no markdown parser).
