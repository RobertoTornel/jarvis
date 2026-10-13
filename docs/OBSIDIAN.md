# Obsidian: reading Claire's memory as a vault

[← Back to Claire](../README.md)

This page describes the **legacy vault format and the export target** — it is **not** the live
store in a normal install. Claire's long-term memory is now **PostgreSQL with pgvector**: the
always-loaded router is one row in `memory_documents` and the notes are further rows in the same
table, embedded in `memory_chunks`. There is no vault directory to open and `CLAIRE.md` does not
exist on disk. [`PGVECTOR.md`](PGVECTOR.md) is the operational runbook and
[`MEMORY.md`](MEMORY.md) explains what memory means.

The Markdown vault format is kept for two reasons. The vault code path still ships and still works
when the memory backend is explicitly set to `vault` (`CLAIRE_MEMORY_BACKEND=vault`), so a rollback
is a flag flip; and the one-way export of the importer writes exactly these files, so you can walk
away with the Markdown. Nothing in Claire reads the directory back. Where the rest of this page
describes behaviour, it is the behaviour of that legacy/export path unless it says otherwise.

What remains genuinely useful about the format — the layout, the frontmatter conventions, wiki
links and the boundary of "safe to sync" — is below. Obsidian itself is not a dependency of
Claire, and Claire does not know or care whether it is installed.

## Layout

```
<vault>/                      ← the exported or legacy directory; point Obsidian at THIS one
├── CLAIRE.md                 ← the router: the document that is always loaded
├── .obsidian/                ← Obsidian's own settings folder (created by Obsidian)
└── notes/                    ← one flat file per fact; the filename is the link target
    ├── dentist.md
    └── car-service.md
```

* `notes/` is **flat**: a note name is one filename, and there are no subdirectories to descend
  into. That is what makes `[[dentist]]` unambiguous in Obsidian, and it is enforced by
  `claire.memory.notes.normalize_note_name()`.
* `.obsidian/` is **optional**. Obsidian creates it the first time it opens the folder; in `vault`
  mode Claire creates it empty for you (and only that, never a preference of her own) when the
  memories browser writes the first note, which is how a fresh vault already looks like a vault.
* The legacy vault holds the router and the notes only. Conversation summaries live in SQLite
  (`claire.db`), which is *not* part of the vault — see [The SQLite half](#the-sqlite-half)
  below.

## Producing these files

The vault format is produced by the **export mode of the importer**, which reads the Postgres
store and writes the Markdown back out:

```bash
python -m claire.memory.migrate_md_to_pg --export
```

`--export` is the one-way export and then exits; nothing in Claire reads the result back. Run it
from the repository root exactly as the import direction is run, and see
[`PGVECTOR.md`](PGVECTOR.md) for the surrounding instructions and the full option table — the same
module does the import direction, so the vault format is both the migration source and the export
target.

## Where the vault is

In the **legacy backend** the vault lives **outside the repository**, in your own data directory:
it is Claire's instructions to herself, not source code, and updating the repository never
overwrites it. In the default `postgres` backend `vault_dir` is not read on the reply path at all.

By default it is `~/.local/share/claire/vault`. Resolution order, first match wins:

| Source | Set it with | Use it when |
|---|---|---|
| An explicit argument | `create_note("dentist", ..., vault_dir="/path")` | Code, tests, one-off scripts |
| The environment | `export CLAIRE_VAULT_DIR="$HOME/Obsidian/Claire"` | One run, or a service override |
| The config key | `"vault_dir": "~/Obsidian/Claire"` in `config.json` | You want it to stick, permanently |
| The default | — | You have not changed anything |

The `vault_dir` config key is the one to reach for when you want a `vault`-mode vault inside your
own Obsidian setup: it needs no environment to be exported and survives reboots. The environment
variable outranks it so a single run can always be redirected without editing anything on disk.

To confirm what Claire actually resolves on the legacy path:

```bash
cd /path/to/jarvis_cloud && PYTHONPATH=src .venv/bin/python -c \
  "from claire.memory.notes import resolve_vault_dir, resolve_notes_dir, resolve_router_path; \
   print(resolve_vault_dir()); print(resolve_notes_dir()); print(resolve_router_path())"
```

Or ask `claire.config`, which is the same resolution the daemon uses:

```bash
cd /path/to/jarvis_cloud && PYTHONPATH=src .venv/bin/python -c \
  "from claire.config import load_settings; print(load_settings().vault_dir)"
```

## Opening it in Obsidian

1. **Find the vault path** with one of the commands above, or use the directory the export wrote.
   On a default legacy install it is `~/.local/share/claire/vault`.
2. In Obsidian: **Open folder as vault** → select that directory. (If Obsidian was already
   running, use *Open another vault* → *Open folder as vault*.)
3. Trust the author when Obsidian asks — it is your own folder, and the only content is markdown
   you or Claire wrote.
4. Read `CLAIRE.md` first: it is the router, and its pointers are the index to everything else.
   The graph view then shows the notes and the links between them.

There is nothing to install and nothing to run: Obsidian is not a dependency of Claire, and
Claire does not know or care whether Obsidian is installed. If you would rather not use
Obsidian at all, any markdown editor, `grep`, or a phone synced through Syncthing reads the same
files.

### A vault inside an existing Obsidian vault

With the backend set to `vault`, point `vault_dir` at a subfolder of your Obsidian vault if you
keep everything in one place:

```json
{ "vault_dir": "~/Obsidian/MyVault/Claire" }
```

Obsidian will show `Claire/CLAIRE.md` and `Claire/notes/*` as ordinary notes, and the wiki links
between them resolve exactly as they do in a dedicated vault.

## Conventions Obsidian reads natively

These are properties of the files the exporter writes and of a `vault`-mode vault; they are not
properties of the Postgres store.

**Frontmatter is flat `key: value`.** That is valid YAML, so Obsidian renders it as a Properties
table instead of printing the block as text:

```markdown
---
title: Dentist
created: 2026-01-31
updated: 2026-02-14
tags: health, home
---

Dr. Ruiz, Calle Mayor 4. Wednesdays at 16:00. See [[car-service]].
```

* Claire writes `title`, `created` and `updated`. Anything else you add by hand is preserved when
  a note is edited (`tags` above, for instance).
* A list value such as `tags` is written **comma-separated**, which is the one list syntax
  Obsidian's properties read as a list. Nested maps and block scalars are deliberately not used:
  the parser in `claire.memory.notes` reads only the flat subset, so a nested key would be
  invisible to Claire even though Obsidian showed it.
* `obsidian.validate_note_document()` reports the ways a document stops being Obsidian-parseable
  — an unclosed `---` block, a tab, an indented or non-`key: value` line, a duplicate key. The
  memories browser shows those problems when you open a note in `vault` mode.

**One note is one file, and the filename is the note's identity.** `title` is a label for people;
`[[name]]` resolves against filenames, which is why renaming the filename changes every link
that used the old name (Obsidian offers to update them for you).

**Pointers are wiki links.** `[[dentist]]` is what an Obsidian user expects and what the graph
draws. The router may also spell a pointer as the plain path `notes/dentist.md`; both forms count
as "the router mentions this note" for `unreferenced_notes()`, so a vault that grew up before
wiki links existed does not read as a pile of orphans. The `[[name|label]]` alias and
`[[name#heading]]` forms work too — the alias is display text, and `#heading` is stripped when
matching a note name.

**A link to a note that does not exist is reported, never invented.** The memories browser marks
an unresolvable link as such; nothing creates a stub note behind your back, because a stub would
be a fact Claire would then read.

## The memories browser

Claire's local panel has a Memory page at `http://127.0.0.1:8765/memory` (linked from the panel's
main page). In the default `postgres` mode it reads and writes the Postgres store, not a vault; in
`vault` mode it lists the router and every note, marks notes the router never mentions, searches
notes and the persisted SQLite summaries with one query, and lets you create, edit and delete a
note on the page.

On the legacy path the browser is not a second implementation: every read and write goes through
`claire.memory.notes` (and `claire.memory.obsidian` for the vault's shape), so what the page
writes is exactly what Claire's parser reads back. The page's reporting of format problems is the
same `validate_note_document()` described above.

The panel binds to loopback only, has no login and is not exposed to the network by default; see
[PANEL_CONVERSATION.md](PANEL_CONVERSATION.md) for its own contract.

## The SQLite half

The vault is the *markdown* half of the **legacy** store. The stores that remain are the SQLite
diary and topic graph (`db_path`, default `~/.local/share/claire/claire.db`) and the PostgreSQL
memory store. Neither belongs inside the vault: Obsidian would not read the database, and
Syncthing copying a live database between machines is a good way to corrupt it.

If you want your facts on your phone, export the vault format (notes are plain files, safe to
sync) and leave the live stores on the machine that writes them.

## Safety model

These are properties of the vault path — the legacy backend and the export — implemented in
`claire.memory.notes`.

* **Memory lives outside the repository.** Nothing under version control contains your facts, and
  a `git pull` cannot overwrite them.
* **Files are written at mode `0600`** (owner read/write only). `create_note` sets the mode on the
  new file, and `update_note` sets it on the temporary file before renaming it into place — neither
  trusts the umask.
* **A note name is one filename.** `../../etc/passwd`, absolute paths and `sub/dir` are rejected
  by `normalize_note_name()` before anything is opened; a symlink inside `notes/` that points
  outside the vault is skipped in listings and refused on read.
* **Edits are atomic.** A note is replaced by writing a sibling temporary file and renaming it
  into place, so an interrupted save leaves the previous note intact rather than a half-written
  one.
* **A refused write writes nothing.** Creating a note never clobbers an existing one, editing
  never silently creates one, and deleting a note that is not there is a named error rather than
  a reported success.
* **The panel never creates the SQLite database.** It opens it read-only (`mode=ro`) for the
  diary and for search.

## Related

* [PGVECTOR.md](PGVECTOR.md) — the runbook for the live PostgreSQL + pgvector store, including the
  export that produces these files.
* [MEMORY.md](MEMORY.md) — what long-term memory means, the router, the 6000-byte cap, and how to
  keep pointers honest.
* [CONFIGURATION.md](CONFIGURATION.md) — the `vault_dir` key among the rest of the settings.
* [PANEL_CONVERSATION.md](PANEL_CONVERSATION.md) — the panel's own contract, including the
  memories browser.
