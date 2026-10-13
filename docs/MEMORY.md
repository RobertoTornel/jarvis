# Memory: the router and the notes

[← Back to Claire](../README.md) · [Runbook: PostgreSQL + pgvector](PGVECTOR.md)

Claire remembers things in two stores that do very different jobs:

| | What it is | Where it lives | How it reaches a reply |
|---|---|---|---|
| **Router and notes** — long-term memory | One small always-loaded router of *pointers*, plus one document per big fact | PostgreSQL with pgvector | the router every turn; notes by hybrid search, on demand |
| **Conversation diary** | One summary of what was said, per `(date_utc, source_app)` | SQLite, full-text search over `summaries_fts` | per-summary search for the current query |

They are separate on purpose. The router and the notes are *Claire's instructions to
herself* — your facts, names and preferences — and they are small, hand-written and worth
carrying into every prompt or looking up by meaning. The diary is the record of what was
actually said, and it is large, automatic and looked up per summary.

This page explains what the two stores mean. [PGVECTOR.md](PGVECTOR.md) is the operational
runbook: installation, schema, migration, export and troubleshooting.

## The router is a pointer file, not a database

The router is one small document of **pointers**. It is the only part of long-term memory
that is always loaded, and it is hard-capped at **6000 bytes**:

* **It is re-read on every request.** Memory is never stale: correcting the router corrects
  Claire's behaviour on the very next thing you say, with **no restart**.
* **Editing it costs zero conversation-history tokens.** Its content is re-rendered into the
  system prompt each turn instead of being appended to the chat history as messages.
  Appending it per turn would grow the history without bound and rot the context;
  re-rendering keeps the cost flat.
* **It carries frontmatter**, which is stored alongside the body:

  ```markdown
  ---
  title: Claire memory router
  role: router
  always_loaded: true
  budget_bytes: 6000
  updated: 2026-01-31
  ---

  # Pointers

  - dentist: see the dentist note
  - car service: booked each spring
  ```

* **Pointers, not content.** Facts belong in notes. The router says *that a note exists* and
  when it is worth opening; it does not carry the fact itself.

A useful rule of thumb: if a line is a fact you would want Claire to act on without being
asked, it belongs in the router or in a note it points to. If it is a fact you would only
want when the topic comes up, it is a note.

## The `## Memory (always loaded)` section

The router is injected into the system prompt under the heading `## Memory (always loaded)`.
Alongside the text, that section can carry a marker when something is wrong:

| Marker | Meaning |
|---|---|
| `[memory-unavailable]` | the memory store could not be read at all |
| `[memory-empty]` | the section rendered, but there was nothing to load |
| `[memory-over-budget]` | the router is past its 6000-byte budget, so it is truncated at a line boundary |

The markers are deliberately visible inside the prompt. A missing database should look like
a missing database, not like Claire quietly forgetting who you are.

## Notes are retrieved, not loaded

A note is one document holding one big fact: a person, a place, a procedure, a preference.
Notes are *not* in the prompt by default. They are found by search when a query is about
them, using hybrid retrieval — vector similarity over the note text plus Postgres full-text
search, with the two rankings fused. That replaces the old habit of following a pointer by
hand: you no longer have to remember which file holds the answer, because retrieval finds it
by meaning as well as by wording.

The router is the exception. It is small and it is always present, which is why notes can be
large and numerous without costing anything until they are needed.

A note the router never names is flagged `unreferenced` in the control panel's Memory page.
That flag is a hint, not an error: it means the router gives no path to that note, so it will
only ever be found by search.

## What "always loaded" means in practice

"Always loaded" describes the *router*, not a database column. The router document is read
from storage on every turn and rendered into the prompt, so it is always present in the
model's context.

The schema does have an `always_loaded` boolean column, and it is currently **decorative**:
no code branches on it. Setting it does not change what is loaded. The behaviour comes from
the router being the router, not from that flag. Do not rely on it.

## The vault is retired

Long-term memory used to be a Markdown vault — an Obsidian-style folder with `CLAIRE.md` and
`notes/*.md`. That is gone. Postgres is the single source of truth, and in a normal install
**`CLAIRE.md` no longer exists on disk**.

The vault code path still ships behind a backend flag, so a rollback is a flag flip:

| Setting | Values |
|---|---|
| `CLAIRE_MEMORY_BACKEND` (environment) | `postgres` (product default), `vault` (legacy) |
| `memory_backend` (`config.json`) | `postgres`, `vault` |

An unrecognised value fails safe to `postgres`. The vault backend is legacy: the pre-existing
test suite pins `vault` because those tests build temporary vaults, and a one-way export can
write the vault format back out — but nothing reads it. [PGVECTOR.md](PGVECTOR.md) has the
flag, the export and the rollback procedure.

## The conversation diary is the other store

The diary is not this store and is not affected by the move to Postgres. It stays in SQLite,
summarised per `(date_utc, source_app)`, and searched through `summaries_fts`. The
`src/claire/utils/vector_*` modules and `claire/memory/db.py` index that diary; pgvector does
not supersede them. Keeping the diary in SQLite and the router and notes in Postgres is
deliberate: different lifetimes, different sizes, different questions asked of them.

## Related

* [PGVECTOR.md](PGVECTOR.md) — the operational runbook: install, schema, migration, export,
  backend selection and troubleshooting.
* [MEMORY_EDITOR_UX.md](MEMORY_EDITOR_UX.md) — how the control panel edits the router and
  the notes.
* [OBSIDIAN.md](OBSIDIAN.md) — the retired vault layout, kept for the rollback path.
* [subsystems/memory.md](subsystems/memory.md) — the developer view of the diary store.
