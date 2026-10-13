# Memory in PostgreSQL + pgvector

[← Back to Claire](../README.md) · [What memory means](MEMORY.md)

This is the operational runbook for Claire's long-term memory store. Claire's memory used
to be a Markdown vault: an Obsidian-style directory with `CLAIRE.md` as the always-loaded
router plus `notes/*.md`. It is now PostgreSQL with the pgvector extension, and Postgres is
the single source of truth.

## What changed, and why

| | Before | Now |
|---|---|---|
| Store | Markdown vault on disk | PostgreSQL 17.11 + pgvector 0.8.7 |
| Always-loaded router | `<vault>/CLAIRE.md` | one row in `memory_documents` with `doc_kind = 'router'` |
| Notes | `<vault>/notes/*.md` | rows in `memory_documents` with `doc_kind = 'note'` |
| Finding a note | follow a pointer by hand | hybrid vector + full-text search |

The vault is **retired**. The vault code path still ships, reachable only by explicitly
setting the backend to `vault`, so a rollback is a flag flip. A one-way manual export can
write the vault format back out, but nothing reads it.

`postgres` is the default because a fresh install has no vault to read, and a missing
database surfaces as a visible `[memory-unavailable]` marker inside the prompt rather than
silent amnesia.

## Architecture

Each turn, the system prompt is rebuilt and the router — and only the router — is read from
Postgres and rendered into it. A search is separate: the query is embedded, run through both
a vector search and a full-text search, and the two rankings are fused.

```
Prompt construction (every turn)
  system_prompt.py
        |
        v
  PostgresMemoryRouter.reload()   reads the router row (doc_kind = 'router')
        |
        v
  budget + line-boundary truncation (6000 bytes)
        |
        v
  "## Memory (always loaded)" section in the system prompt

Retrieval (a query)
  query
    |
    v
  embedder: nomic-embed-text, 768 dimensions
    |
    +---------------------------------+
    v                                 v
  HNSW vector search            full-text search
  (vector_cosine_ops)           GIN over the generated tsv column
    |                                 |
    +--------------> RRF <------------+
                (_RRF_K = 60)
                     |
                     v
       hits: content, doc_kind, heading, match, name, score
```

### The router and the prompt

`claire/memory/pg_router.py` defines `PostgresMemoryRouter`, a subclass of the file-backed
`MemoryRouter` in `claire/memory/router.py` that overrides only `reload()` to read the
router row. Subclassing is deliberate: budget, line-boundary truncation and the
over-budget warning stay identical to the file router. Its header text differs on purpose —
the file router's header told the model to "edit CLAIRE.md", which no longer exists, so the
Postgres router says to edit memory on the control panel's Memory page, and keeps the same
promise that a change takes effect on the next turn with no restart.

The router has a 6000-byte budget. The memory section is injected into the system prompt
under the heading `## Memory (always loaded)`, and its failure markers are
`[memory-unavailable]`, `[memory-empty]` and `[memory-over-budget]`.

### Embeddings

`claire/memory/embedder.py` is the single entry point for memory embeddings. The model is
`nomic-embed-text` at **768 dimensions**, reached through `claire.llm.get_embedding_backend`.

In the live configuration embeddings route through OmniRouter, because the
`embedding_provider` config key is empty and therefore inherits `llm_provider:
openai_compatible`. It is currently bit-identical to calling Ollama directly, but switching
to a different embedding provider would mix incomparable vector spaces. **Recommendation:
pin `embedding_provider: "ollama"`** to prevent an accidental silent switch.

Measured embedding latency is 6–15 ms warm but **2.18 s cold**, so the per-turn path clamps
the timeout to about 1 second; a cold backend cannot stall a reply.

### Retrieval

Retrieval is hybrid: vector similarity plus Postgres full-text search, fused with Reciprocal
Rank Fusion (`_RRF_K = 60`). If no query vector can be produced — the embedding backend is
down — it degrades to keyword-only rather than failing.

```python
search(query, k, *, query_vec=None, dsn=None) -> list[dict]
# in claire/memory/pg.py
```

Each result dict has the keys `content`, `doc_kind`, `heading`, `match` (`"hybrid"` or
`"keyword"`), `name` and `score`.

At very low row counts the HNSW index will legitimately **not** be used: the planner prefers
a sequential scan. That is correct behaviour, not a misconfiguration, and the index only
shows up in an index scan with `SET enable_seqscan = off`.

## Prerequisites and installation

You need PostgreSQL 17.11 with the pgvector 0.8.7 extension, and Docker. The deployment
files are in `deploy/pgvector/`:

| File | What it is |
|---|---|
| `docker-compose.yml` | the `claire-pg` service: image `pgvector/pgvector:pg17`, volume `claire-pgdata17` |
| `setup.sh` | idempotent setup; exits 0 when already set up |
| `README.md` | the same install, plus stop, reset and version-upgrade notes |

Source the DSN file first, because the compose file reads `CLAIRE_PG_PASSWORD` from the
environment and refuses to start without it:

```bash
set -a; . ~/.config/claire/pg.env; set +a
```

If the Docker Compose *plugin* is installed:

```bash
docker compose -f deploy/pgvector/docker-compose.yml up -d
deploy/pgvector/setup.sh
```

On this host the plugin is **not** installed, so use plain `docker`. This reproduces the
same container:

```bash
set -a; . ~/.config/claire/pg.env; set +a
docker run -d --name claire-pg --restart unless-stopped \
  -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=claire -e POSTGRES_DB=claire \
  -e POSTGRES_PASSWORD="$CLAIRE_PG_PASSWORD" \
  -v claire-pgdata17:/var/lib/postgresql/data \
  pgvector/pgvector:pg17
deploy/pgvector/setup.sh
```

`setup.sh` creates `claire_test` if it is missing and enables `vector` in both databases. It
never drops anything, and it exits 0 when everything is already in place.

## DSN configuration

Connection strings live in `~/.config/claire/pg.env` — mode 0600, never committed:

| Variable | Database |
|---|---|
| `CLAIRE_PG_DSN` | production database `claire` |
| `CLAIRE_PG_TEST_DSN` | test database `claire_test` |

Systemd user units get the file through a drop-in
`~/.config/systemd/user/<unit>.service.d/pg-env.conf` whose contents are:

```
EnvironmentFile=-%h/.config/claire/pg.env
```

That drop-in exists for `claire.service`, `claire-panel.service` and `claire-desktop.service`.

Resolution order in `claire/memory/pg.py::resolve_dsn()` is: explicit argument, then
`CLAIRE_PG_DSN`, then `config.json`'s `pg_dsn`, then `pg.env`, then assembled parts.

**Never log or print a DSN.** It contains the password.

## Backend selection

`CLAIRE_MEMORY_BACKEND` is read in `claire/system_prompt.py::_memory_backend()`:

| Source | Key | Values | Notes |
|---|---|---|---|
| Environment | `CLAIRE_MEMORY_BACKEND` | `postgres` (product default), `vault` | re-read every turn; wins over config |
| Config file | `memory_backend` in `config.json` | `postgres`, `vault` | the module caches this lookup for the process |

An unrecognised value fails safe to `postgres`.

The pre-existing test suite pins `vault` explicitly in `tests/conftest.py` because those
tests build temporary vaults; tests that exercise Postgres opt in with
`monkeypatch.setenv("CLAIRE_MEMORY_BACKEND", "postgres")`.

## Schema

Created by `claire/memory/pg.py::bootstrap_schema()`. `SCHEMA_VERSION = 2`, recorded in the
`schema_migrations` table.

### `memory_documents` — one row per document

| Column | Purpose |
|---|---|
| `id` | surrogate primary key |
| `source_path` | UNIQUE; the document's identity and the upsert key used by the importer |
| `doc_kind` | `'router'` or `'note'` |
| `name` | the document's short name |
| `title` | display title |
| `body` | the Markdown body |
| `content_sha256` | hash of the body, for cheap change detection |
| `frontmatter` | parsed frontmatter, `jsonb` |
| `frontmatter_raw` | the original frontmatter bytes, `text`; kept so export round-trips byte-for-byte, because `jsonb` loses key order |
| `always_loaded` | `boolean`; currently decorative — no code branches on it |
| `budget_bytes` | nullable `int`; the per-document byte budget |
| `source_mtime` | `timestamptz`; the source file's mtime at import |
| `source_bytes` | the source file's size at import |
| `imported_at` | when the row was first written |
| `updated_at` | when the row was last written |

### `memory_chunks` — one row per embeddable slice

| Column | Purpose |
|---|---|
| `id` | surrogate primary key |
| `document_id` | FK to `memory_documents`, `ON DELETE CASCADE` |
| `chunk_index` | position within the document; UNIQUE together with `document_id` |
| `heading` | the heading the slice sits under |
| `content` | the slice text |
| `content_sha256` | hash of the slice, used to skip re-embedding unchanged content |
| `char_start`, `char_end` | offsets of the slice in the document body |
| `token_estimate` | rough token count, for budgeting the recall block |
| `embedding` | `vector(768)`; NULL until the row has been embedded |
| `embed_model` | the embedding model that produced the vector |
| `embed_provider` | the provider that produced the vector |
| `embedded_at` | when the vector was written |
| `tsv` | a generated `tsvector` over `content`, which is what the keyword half of the search matches |

### Indexes

| Index | Definition | Why |
|---|---|---|
| HNSW | `memory_chunks_embedding_hnsw_idx` on `embedding` with `vector_cosine_ops`, `m=16`, `ef_construction=64` | the vector half of the search |
| GIN | `memory_chunks_tsv_idx` on the generated `tsv` column | the keyword half of the search |
| btree, unique | `memory_chunks_document_id_chunk_index_key` on `(document_id, chunk_index)` | one row per slice position, so a re-import replaces rather than duplicates |
| btree | `memory_chunks_document_id_idx` on `(document_id)` | cascade deletes and per-document reads |
| btree, partial | `memory_chunks_pending_embedding_idx` on `(document_id, chunk_index)` **where `embedding IS NULL`** | finds the rows still needing a vector, which is what makes a backfill cheap |
| btree, unique partial | `memory_documents_one_router_idx` on `(doc_kind)` **where `doc_kind = 'router'`** | the database itself enforces that there is at most one router |
| btree, unique | `memory_documents_source_path_key` on `(source_path)` | the identity the importer upserts on |

`schema_migrations` records `(version, name, checksum, applied_at)`; the table is
currently at version `2`, named `pgvector_memory_v2`.

## Migration from the vault

The tool is `python -m claire.memory.migrate_md_to_pg`. Run it from the repository root with
the project virtualenv, after sourcing the DSN file. Always dry-run first:

```bash
cd /home/mana/Documents/jarvis_cloud
set -a; . ~/.config/claire/pg.env; set +a

# 1. report only — writes nothing
.venv/bin/python -m claire.memory.migrate_md_to_pg --dry-run

# 2. the real import
.venv/bin/python -m claire.memory.migrate_md_to_pg
```

| Option | Effect |
|---|---|
| `--dry-run` | report what would change; write nothing |
| `--vault DIR` | use this vault directory instead of the resolved one |
| `--dsn DSN` | import into this DSN instead of the resolved one |
| `--export` | write the vault format back out instead of importing |
| `--no-bootstrap` | do not create or update the schema first |
| `--json` | machine-readable report |

The import is idempotent and keyed on `source_path`. It reports four counts:

| Field | Meaning |
|---|---|
| `created` | the document was not present before and was inserted |
| `updated` | the document was present but its stored content differs |
| `unchanged` | the document is identical to what is stored; nothing was written |
| `skipped` | the document was not imported |

A **frontmatter-only edit counts as `updated` without re-embedding any chunk**, because
chunks are compared by content hash.

## Exporting back to Markdown

`--export` writes the vault format back out and is byte-identical to the original file
(verified: 914 bytes in, 914 bytes out). The export is one-way — nothing in Claire reads it
back. It exists so you can walk away with the Markdown if you want it.

## Day-to-day operations

### Is it up?

```bash
docker ps --filter name=claire-pg
docker exec claire-pg pg_isready -U claire -d claire
```

### What is in it?

```bash
docker exec claire-pg psql -U claire -d claire -c \
  "select doc_kind, count(*) from memory_documents group by doc_kind;"
docker exec claire-pg psql -U claire -d claire -c \
  "select count(*) from memory_chunks;"
docker exec claire-pg psql -U claire -d claire -c \
  "select * from schema_migrations;"
```

### Running the tests

Tests must only ever target `claire_test`, never the production database:

```bash
cd /home/mana/Documents/jarvis_cloud
set -a; . ~/.config/claire/pg.env; set +a
.venv/bin/python -m pytest tests/test_memory_pg.py tests/test_memory_pg_router.py -q -p no:cacheprovider
```

The Postgres tests skip themselves when the test database is unreachable, and refuse to run
destructively against the production DSN.

### After an embedding-model or provider change

The importer compares chunk content hashes, so re-running the migration after an embedding
change reports `unchanged` and **does not** re-embed anything. Each chunk records its
`embed_model` and `embed_provider`, so vectors from different models are at least
distinguishable, but vectors from different models are not comparable. A model change is
therefore a planned rebuild, not a re-run of the import; this runbook does not define a
single re-embed command. Pin `embedding_provider: "ollama"` so the embedding model cannot
change by accident.

### Vacuum and analyse

Nothing in Claire requires a scheduled `VACUUM`; autovacuum normally keeps up. After a large
import, a manual `VACUUM (ANALYZE) memory_chunks;` refreshes statistics and reclaims space.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `[memory-unavailable]` in replies | the memory section could not be read: the database is unreachable, or no DSN resolved | start `claire-pg`; check `~/.config/claire/pg.env`; check the unit's `pg-env.conf` drop-in; restart the unit |
| `relation "memory_chunks" does not exist` | connected to a database where `bootstrap_schema()` has not run | run the migration tool without `--no-bootstrap`, or call `claire.memory.pg.bootstrap_schema()`; confirm you are pointed at `claire` / `claire_test` and not another database |
| `operator does not exist: vector <=> double precision[]` | the query vector reached SQL as a plain float array rather than `vector`, or its dimension does not match the `vector(768)` column | make sure the embedding backend returns the 768-dimension `nomic-embed-text` vector and that it is adapted to `vector`; pin `embedding_provider` so the dimension cannot change silently |
| slow first embedding | the model is cold: 6–15 ms warm, 2.18 s cold while it loads | expected; the per-turn path clamps the embedding timeout to about 1 second so a cold backend cannot stall a reply. Warm the backend if you care about the first retrieval |
| HNSW index not used | at very low row counts the planner prefers a sequential scan | none. This is correct behaviour; verify with `SET enable_seqscan = off` if you want to see the index scan |
| panel shows a database error state | the database is unreachable from the panel process | the page renders a named error state with a fix rather than a 500 — follow the fix it prints (start the container, correct the DSN, run `setup.sh`) |
| `[memory-empty]` | the memory section rendered but there was nothing to load; the router row is missing or its body is empty | edit the router on the panel's Memory page. The router cannot be deleted through the panel, only edited |
| `[memory-over-budget]` | the router body exceeds its 6000-byte budget | shorten the router on the panel's Memory page; the warning behaves as it did for the file router |

## The control panel

`claire/panel/server.py`'s `RealPanelState` builds the same `MemoryNotesStatus`,
`MemoryNoteView` and `MemorySearchStatus` dataclasses whichever backend is active, so every
renderer and invariant is unchanged. In Postgres mode:

| Method | Behaviour |
|---|---|
| `memory_notes()` | lists router and notes; a note the router never names is flagged `unreferenced` |
| `memory_note(name)` | opens one document |
| `memory_search(query)` | hybrid retrieval |
| `save_note(...)` | upserts and re-chunks |
| `delete_note(name)` | deletes a note |

The router cannot be deleted through the panel; it must be edited. If the database is
unreachable the page renders a named error state with a fix, never a 500.

## What this store deliberately does not do

It does **not** index the conversation diary. The diary is the separate SQLite store with
`summaries_fts`, searched per summary. The `src/claire/utils/vector_*` modules and
`claire/memory/db.py` index that diary, not this one. pgvector does not supersede them: the
two stores hold different things and are read for different reasons.

## Related

* [MEMORY.md](MEMORY.md) — what the two memory stores mean, for a first read.
* [deploy/pgvector/README.md](../deploy/pgvector/README.md) — install, stop, reset and
  major-version upgrade notes.
* [PGVECTOR_DESIGN.md](PGVECTOR_DESIGN.md) — the pre-implementation design review.
* [MEMORY_EDITOR_UX.md](MEMORY_EDITOR_UX.md) — the panel's Memory page.
* [OBSIDIAN.md](OBSIDIAN.md) — the retired vault, kept for the rollback path.
