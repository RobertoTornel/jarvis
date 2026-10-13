# Migrating Claire's memory from the Markdown vault to PostgreSQL + pgvector

Status: **DESIGN — not implemented.** This document specifies the target architecture, the
exact call sites to change, and a cutover that cannot leave the assistant without memory.

Branch: `claire-rewrite` (HEAD `cc97a9c`). Author: systems architecture review.

---

## 0. Verified ground truth (measured during this review, not assumed)

Every value below was observed on this machine at review time. Where a claim comes from
code rather than execution, the `file:line` is given.

| Fact | Observed value | How verified |
|---|---|---|
| Running container | `pgvector/pgvector:pg17`, `Up (healthy)`, `127.0.0.1:5432->5432/tcp` | `docker ps --filter name=claire-pg` |
| Server version | **PostgreSQL 17.11** (Debian 17.11-1.pgdg12+2) | `select version()` |
| pgvector | `vector 0.8.7`, **both** `hnsw` and `ivfflat` AMs present | `select extversion from pg_extension`, `select amname from pg_am` |
| Data volume | `claire-pgdata17` → `/var/lib/postgresql/data` | `docker inspect claire-pg` |
| Tables in DB `claire` | **0** (`information_schema.tables where table_schema='public'`) | psql |
| DSN file | `~/.config/claire/pg.env`, mode `0600`, owner `mana` | `stat` |
| Embedding model | `nomic-embed-text:latest`, 274 MB, `nomic-bert`, 137M params, F16, context 2048 | `GET /api/tags` |
| **Embedding dimension** | **768** | measured on all three routes below |
| Embedding route (live) | `config.json` has **no** `embedding_provider` / `embedding_model`, and `llm_provider = "openai_compatible"` → `llm_base_url = http://127.0.0.1:20129/v1` (OmniRouter) | `~/.config/claire/config.json`; resolution at `src/claire/config.py:1050-1058` |
| Cross-route safety | OmniRouter `nomic-embed-text` ≡ ollama `/api/embed` ≡ ollama `/api/embeddings`: **bit-identical**, `cos = 1.000000`, `A == B` is `True` | embedded the same sentence on all three routes and compared |
| Embed latency | `/api/embed` cold **2.18 s** (model load); `/api/embeddings` warm **0.015 s**; OmniRouter warm **0.048 s** | `curl -w '%{time_total}'` |
| Runtime model | **synchronous + threads.** `src/claire/reply/engine.py` has **0** `async def` and 17 `def`; asyncio appears only in `senses/browser.py`, `tools/external/mcp_client.py`, `mcp_runtime.py`; `aiohttp` is imported nowhere | `grep -c 'async def'`, `grep -rln asyncio` |
| Vault contents | `CLAIRE.md` (914 B, mode 0600) + `notes/` **containing zero files** + `.obsidian/` | `ls`, `find … -name '*.md' \| wc -l` → `1` |
| `sha256(CLAIRE.md)` | `1955b0e665193396bee2699e6737068ef17c4d0bfec5217ec8dddf1057516152` | `sha256sum` |
| Existing SQLite store | `~/.local/share/claire/claire.db`, 86 KB; tables `conversation_summaries`=6 rows, `memory_nodes`=4, `faiss_vector_store`=2, `summaries_fts`=6, `meals`=0 | `sqlite3` read-only |
| Service units | `claire.service`, `claire-panel.service`, `claire-desktop.service` all **active (running)** | `systemctl --user is-active` |
| **`pg.env` is not wired to the services** | Nothing in the repo references `pg.env`; neither unit lists it in `EnvironmentFile`. Units load only `-~/.config/claire/claire.env` | `grep -rn 'pg\.env\|CLAIRE_PG_DSN'`, `grep -n EnvironmentFile ~/.config/systemd/user/*.service` |

> The container was `pg16` when this review began and was replaced with `pg17` **during** the
> review; the table above is the state after the swap. All values were re-read after the swap.

Two consequences of the table deserve emphasis before anything else:

**(a) The entire migration currently moves one row.** The vault holds `CLAIRE.md` and zero
notes. The import is 1 document, ~1 chunk, 804 bytes of body. This is why the cutover below is
low-risk and why index tuning is forward-looking rather than load-bearing.

**(b) The service units cannot see the DSN.** `pg.env` exists and is correct and 0600, but
`claire.service:76` and `claire-panel.service:68` load only `claire.env`. Until that is fixed,
Postgres-backed memory would work when run by hand and fail under systemd. This is a required
step in §5, not an optional nicety.

---

## 1. Current architecture as-is

### 1.1 Read path A — the always-loaded router (the only vault read on the hot path)

```
src/claire/reply/engine.py:1735   build_system_prompt(_assistant_name, load_default_router=True)
  └─ src/claire/system_prompt.py:172  load_router_from_vault()
       └─ src/claire/memory/router.py  load_router()            # module entry point
            └─ MemoryRouter.render_for_prompt()   router.py:287
                 └─ self.reload()                 router.py:262   ← re-reads the file
                      └─ _read_source(self.path)  router.py:167   ← disk I/O, every call
```

`render_for_prompt()` deliberately does **not cache** (`router.py:287-289`, and the module
docstring at `router.py:9-13` states the intent: correcting the file corrects behaviour on the
next utterance with no restart). So the cost is **one `stat` + one `read` of `CLAIRE.md` per
turn**, and the rendered text is placed into the system prompt rather than the conversation
history, so it costs **zero history tokens** and cannot grow the context across turns.

**Measured cost per turn** (executed against the live vault via `.venv/bin/python`):

| Quantity | Bytes | Notes |
|---|---|---|
| `CLAIRE.md` on disk | 914 | includes ~110 B of YAML frontmatter |
| `MemoryRouter.size_bytes` | **804** | **body only** — `router.py:242-245`: *"Real byte size of the body (what the prompt would pay for)"*; frontmatter is excluded |
| `MEMORY_HEADER` | 244 | `router.py:82-87`; **not counted against the budget** |
| `render_for_prompt()` | **1050** | `MEMORY_HEADER` + `"\n\n"` + body |
| `build_memory_section()` | **1076** | adds `## Memory (always loaded)` (`system_prompt.py:130`, `:249`) |
| Full system prompt | **6705** | `build_system_prompt('Claire', load_default_router=True)` |
| **Memory share of prompt** | **16.0 %** | 1076 / 6705 |
| Approx. prompt tokens | **~1670** | `chars/4` approximation, for scale only |

### 1.2 `budget_bytes` and `always_loaded` semantics

**`budget_bytes`** is the one frontmatter key the code actually honours.

- Default `ROUTER_BUDGET_BYTES = 6000` (`notes.py:93`); a duplicated defensive default
  `_DEFAULT_ROUTER_BUDGET_BYTES = 6000` at `system_prompt.py:135`.
- Per-file override: `MemoryRouter.budget_bytes` (`router.py:238-240`) coerces the frontmatter
  value through `_coerce_budget` (`router.py:185`); anything unusable silently means "default".
- The cap applies to the **body**, not the file: `over_budget` is
  `self.size_bytes > self.budget_bytes` (`router.py:249-250`) and `size_bytes` is body-only.
  A maxed-out router therefore renders `244 (header) + 2 + 6000 ≈ 6246` bytes.
- Truncation is at a **whole-line boundary** (`_truncate_body`, `router.py:195-219`) so a fact is
  never cut in half; a first line larger than the budget means nothing is kept.
- Going over the cap is **loud, not silent**: `_budget_warning` (`router.py:93`) prepends a
  72-`!` banner (`_BUDGET_BANNER`, `router.py:91`) naming the real size, the budget and the bytes
  dropped.
- Live value today: frontmatter `budget_bytes: 6000`, effective `6000`, body 804 → **not over
  budget**, so no truncation path runs.

**`always_loaded` is documentation, not behaviour.** It is written into the template
(`router.py:127`) and parsed into `frontmatter`, and tests assert it round-trips
(`tests/test_memory_router.py:77,327,390`). But **no code path branches on it** — a repo-wide
grep for `always_loaded` returns only the template string, the docs, and test assertions. What
actually makes the router always-loaded is that `engine.py:1735` unconditionally calls
`build_system_prompt(..., load_default_router=True)`, i.e. the *filename* is the contract, and
`always_loaded: true` is a comment the owner reads. §2 keeps the column for continuity; §6 asks
whether it should become real.

### 1.3 Read path B — notes (cold; never on the hot path)

There is **no LLM-facing tool that reads a note.** A grep across `src/claire/tools/` for
vault/note readers returns only unrelated matches (`timer.py`'s `persistence_note`,
`web_search.py` comments, `senses.py`'s `_note` UI helper). Notes are reachable by the model
**only as prose pointers inside `CLAIRE.md`** — e.g. the live file contains
`- dentist: notes/dentist.md - Dr. Ruiz, Wednesdays at 16:00.` The model reads the pointer and
has no mechanism to fetch the note body. This is the central functional gap the migration closes.

Consequently, today's note readers are all human-driven:

| Call site | Function | What it does |
|---|---|---|
| `panel/server.py:1984` | `notes_module.list_notes` | panel memory page listing |
| `panel/server.py:2019` | `notes_module.unreferenced_notes` | notes with no pointer in the router body |
| `panel/server.py:2070` | `notes_module.read_note` | open one note in the panel |
| `panel/server.py:2083` | `notes_module.list_notes` | validate note name against existing set |
| `panel/server.py:2147` | `notes_module.search_notes` | the panel's note search box |
| `panel/server.py:1982` | `obsidian_module.describe_vault` | layout/status |
| `platform/service.py:566` | `notes.resolve_vault_dir` | self-check probe "is the vault present and writable?" |

`search_notes` (`notes.py:585`) is **keyword-only by construction** and says so
(`notes.py:592-595`): lowercased word tokens, `+1` per distinct token on a line, `+4` per token
in the title, `+5` for a whole-query substring, ties broken by note name then line number. It
walks every `*.md` file and reads every line of each (`notes.py:614-620`). It is `O(files ×
lines)` per query with no index and no synonym handling — a query for "garage" will not find a
note that only says "car service".

### 1.4 Read path C — the diary/graph memory (already vectorised, and *not* the vault)

This is a **separate** subsystem that must not be confused with vault memory:

```
conversation.py:1963  search_conversation_memory(db, cfg, search_query=…)
  └─ conversation.py:74   _embed_text(text, cfg, timeout_sec, task_type)
       └─ llm/factory.py:128  get_embedding_backend(cfg).embed(text, cfg.embedding_model, …)
            └─ llm/ollama.py:340  OllamaBackend.embed() → POST {base_url}/api/embeddings
  └─ memory/db.py:183     Database.search_hybrid(query, vec_json, top_k)
       └─ utils/fast_vector_store.py  FAISSVectorStore.search()   (or PythonVectorStore)
```

It indexes `conversation_summaries` rows (6 today) keyed by `summary_id`, falling back to FTS
when the embedding is unavailable (`conversation.py:2005-2013`). **This subsystem stays as it
is** — see §3.2.

### 1.5 Write paths

| Writer | Location | Mechanism | Trigger |
|---|---|---|---|
| Router creation | `router.py:319-327` | `target.parent.mkdir(parents=True)`, then `target.write_text(...)` | `ensure_router()` **only when the file is missing**; created from `router_template()` (`router.py:115`) |
| Note creation | `notes.py:867` | `os.fdopen(handle_fd, "w", …)` — atomic temp-then-rename; parent dirs via `mkdir` at `notes.py:793`, `:851` | `create_note()` — panel only |
| Panel note save | `panel/server.py:2207` (`RealPanelState.save_note`) | delegates to `notes.create_note` | owner clicks save in the panel |
| Panel note delete | `panel/server.py:2248` (`RealPanelState.delete_note`) | unlink | owner clicks delete |
| Vault scaffolding | `panel/server.py:2227` → `obsidian_module.ensure_vault` | `mkdir` vault, `notes/`, `.obsidian/` | panel "create vault" |

The abstract seam at `panel/server.py:1412-1425` (`PanelState.save_note` / `.delete_note` raising
`PanelStateError`) is a deliberate refuse-by-default contract; the real implementations are
`RealPanelState.save_note` at `:2207` and `.delete_note` at `:2248`. `engine.py` **never writes**
vault memory: it only reads.

### 1.6 Summary: what changes and what does not

| Path | Today | After migration |
|---|---|---|
| Router read (hot) | disk read of `CLAIRE.md` every turn, 804 B body | **one indexed `SELECT` on `memory_documents`** for the `always_loaded` row; truncation logic unchanged |
| Note read | not reachable by the model; panel-only keyword search | **semantic + FTS hybrid retrieval into the prompt** (§2.5) |
| Note write | files, panel only | **`INSERT`/`UPDATE` in Postgres**, panel + a new explicit tool |
| Diary/graph vector search | FAISS over SQLite `conversation_summaries` | **unchanged** (§3.2) |
| Embedding client | `get_embedding_backend(cfg).embed(...)` | **the same call — reused, not duplicated** (§3.1) |

---

## 2. Proposed schema

### 2.1 Design decisions, stated up front

1. **`vector(768)` is not configurable.** `EMBEDDING_DIMENSION = 768` is an existing storage
   contract (`src/claire/utils/embedding_vector.py:31` and its docstring at `:3-25`), already
   baked into the sqlite-vss `vss0(vec(768))` declaration and the FAISS index width
   (`memory/db.py:129-135`). My measurement (§0) confirms 768 on every route. The column is
   `vector(768)` with no `dimension` setting anywhere; a model change is a **re-embed**, never a
   `ALTER COLUMN`. `normalise_embedding(vector, 768)` (`embedding_vector.py:34`, raises on a
   width mismatch) is the write-side gate.
2. **Chunking is implemented but is a no-op on day one.** With 0 notes, the import creates one
   document and one chunk. The chunking rule exists so that when notes arrive it is already
   right; §2.3 gives the rule.
3. **The router is stored once and used twice.** Its body is injected whole into the prompt each
   turn (preserving `budget_bytes` semantics exactly) *and* it carries a chunk so it is
   searchable. One row, two consumers — not two copies.
4. **Hybrid retrieval, mirroring the existing stack.** The repo already does reciprocal-rank
   fusion (`memory/db.py:95 _reciprocal_rank_fusion`) combining vector hits with FTS hits and
   falling back to FTS-only when the embedding is unavailable. The Postgres query does the same
   (§2.5), so behaviour and failure semantics stay familiar, and a 768-dim vector is never the
   single point of failure for recall.

### 2.2 Extensions and migrations

```sql
CREATE EXTENSION IF NOT EXISTS vector;   -- 0.8.7 present already; idempotent

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer      PRIMARY KEY,
    name        text         NOT NULL,
    checksum    text         NOT NULL,          -- sha256 of the migration SQL
    applied_at  timestamptz  NOT NULL DEFAULT now()
);
```

The repo's existing migration pattern is a named-ledger table with an idempotent `INSERT`
(`memory/db.py:14-19 diary_index_migrations`, applied at `:165-168`). `schema_migrations` is the
Postgres equivalent, with a `checksum` added so a mutated migration is detected rather than
silently skipped. The `vector` extension is created at `version 1` and never re-created
(`IF NOT EXISTS`).

### 2.3 Documents

```sql
CREATE TABLE IF NOT EXISTS memory_documents (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_path    text        NOT NULL UNIQUE,   -- vault-relative: 'CLAIRE.md', 'notes/dentist.md'
    doc_kind       text        NOT NULL
                               CHECK (doc_kind IN ('router','note')),
    name           text        NOT NULL,          -- 'CLAIRE', or the note stem 'dentist'
    title          text        NOT NULL DEFAULT '',
    body           text        NOT NULL,          -- markdown with frontmatter removed
    frontmatter    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    always_loaded  boolean     NOT NULL DEFAULT false,
    budget_bytes   integer,                       -- router only; NULL for notes
    content_sha256 text        NOT NULL,          -- sha256(body); the idempotency key
    source_mtime   timestamptz,                   -- provenance of the one-shot import
    source_bytes   integer,                       -- file size at import, for verification
    imported_at    timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- The hot path: fetch the always-loaded router. At most one row by construction.
CREATE UNIQUE INDEX IF NOT EXISTS memory_documents_one_router_idx
    ON memory_documents (doc_kind)
    WHERE doc_kind = 'router';
```

Notes on the columns:

- `source_path` is `UNIQUE` and is **what makes the import idempotent** (§4). It stays after the
  vault is retired because the export command needs it to reconstruct the directory tree, and
  because it is the only remaining link to "which file did this come from".
- `body` excludes YAML frontmatter, because that is exactly what `MemoryRouter` treats as the
  budgeted payload (`size_bytes`, `router.py:242-245`) and what `Note.body` returns
  (`notes.py:439`, via `strip_frontmatter` at `notes.py:281`). Storing frontmatter inline in `body` would
  change the byte accounting and break the 6000-byte cap's meaning.
- `frontmatter jsonb` preserves every key verbatim, so keys the schema does not model are not
  lost. `parse_frontmatter` (`notes.py:248`) yields a flat string→string map; it becomes jsonb
  unchanged. This is a **deliberate superset**: `always_loaded` and `budget_bytes` are also
  promoted to typed columns because the hot path reads them on every turn and should not pay a
  jsonb extraction.
- `always_loaded boolean` exists to make the semantics real (today they are decorative, §1.2).
  Only the router row is `true`.
- `content_sha256` is `sha256(body)`, not of the whole file: a frontmatter-only edit (e.g.
  bumping `updated:`) must not trigger re-embedding of unchanged prose. Frontmatter changes are
  detected separately by comparing the jsonb.

### 2.4 Chunks

```sql
CREATE TABLE IF NOT EXISTS memory_chunks (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id    bigint      NOT NULL
                               REFERENCES memory_documents(id) ON DELETE CASCADE,
    chunk_index    integer     NOT NULL,          -- 0-based, stable within the document
    heading        text        NOT NULL DEFAULT '', -- nearest preceding markdown heading
    content        text        NOT NULL,
    char_start     integer     NOT NULL,
    char_end       integer     NOT NULL,
    token_estimate integer     NOT NULL,          -- chars/4, advisory only
    content_sha256 text        NOT NULL,          -- sha256(content); skips redundant re-embedding
    embedding      vector(768),                   -- NULL = not yet embedded
    embed_model    text,                          -- e.g. 'nomic-embed-text'
    embed_provider text,                          -- e.g. 'openai_compatible' | 'ollama' | 'gemini'
    embedded_at    timestamptz,
    tsv            tsvector    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    UNIQUE (document_id, chunk_index)
);
```

`embed_model` + `embed_provider` are recorded per row on purpose. §0 showed the live embedding
route is OmniRouter (via `llm_provider`), and §3.1 notes that pointing `embedding_provider` at
Gemini would produce an **incomparable vector space**. Without these two columns that mistake is
invisible; with them, a mismatch is one `GROUP BY` away:

```sql
SELECT embed_provider, embed_model, count(*),
       count(*) FILTER (WHERE embedding IS NULL) AS unembedded
FROM memory_chunks GROUP BY 1, 2;
```

`content_sha256` allows re-import to re-embed only genuinely changed chunks rather than the
whole document (§4.2). `chunk_index` is stable so the unique constraint doubles as the
idempotency guard for chunk rows.

`tsv` is generated with the `'english'` text-search configuration. **This is a real assumption**
— see §6, risk R7. `'simple'` is the safe fallback if the vault ever holds non-English prose.

#### Chunking rule

Applied to `doc_kind = 'note'` only; the router is always exactly one chunk.

1. Split the body at markdown **H2/H3** boundaries (`^#{2,3}\s`), matching the section style the
   live `CLAIRE.md` already uses.
2. If a section is **≤ 1500 chars**, it is one chunk.
3. If a section is **> 1500 chars**, split further at blank-line paragraph boundaries into pieces
   of ≤ 1500 chars with **~15 % overlap** (carry the trailing paragraph forward), so a fact
   spanning a paragraph break is still retrievable.
4. Never split mid-line and never split inside a fenced code block.
5. `heading` is the nearest preceding heading, so a retrieved chunk can be presented with its
   context ("About you", "Standing preferences").
6. `char_start`/`char_end` are byte-accurate offsets into `body`, so a future "open in editor"
   panel action can highlight the exact span.

*This 1500-char figure is a recommendation, not a measurement.* It is chosen so that ~2 chunks
fit the retrieval budget in §2.6 with room for the surrounding prompt. Confirm against real
notes once any exist.

### 2.5 Index choice — HNSW, and why not IVFFlat

```sql
-- Approximate nearest neighbour, cosine distance.
CREATE INDEX IF NOT EXISTS memory_chunks_embedding_hnsw_idx
    ON memory_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Work queue: chunks still needing an embedding.
CREATE INDEX IF NOT EXISTS memory_chunks_pending_embedding_idx
    ON memory_chunks (document_id, chunk_index)
    WHERE embedding IS NULL;

-- Keyword half of the hybrid query.
CREATE INDEX IF NOT EXISTS memory_chunks_tsv_idx ON memory_chunks USING gin (tsv);

-- Cascade lookups.
CREATE INDEX IF NOT EXISTS memory_chunks_document_id_idx ON memory_chunks (document_id);
```

**HNSW over IVFFlat, for three concrete reasons:**

1. **IVFFlat needs training data and degrades badly on a tiny corpus.** IVFFlat builds `lists`
   centroids by k-means at `CREATE INDEX` time. With the ~1–100 rows this table will hold, any
   `lists` value large enough to be useful leaves most lists empty and recall collapses; a
   `lists` value small enough to be safe is pointless. HNSW has **no training step** and behaves
   sensibly from the first row.
2. **HNSW handles incremental inserts correctly.** Claire's memory grows by single-row inserts
   (one new note, one edited note). HNSW inserts into the graph directly; IVFFlat assigns to the
   nearest existing centroid, and quality drifts until a `REINDEX`.
3. **It matches the precedent already in this repo.** `utils/fast_vector_store.py:105-110` uses
   `faiss.IndexHNSWFlat(dimension, 32)` with `efConstruction = 200`, `efSearch = 50` once the
   corpus passes 10 000 vectors, and exact `IndexFlatIP` below that. HNSW is the team's settled
   choice for this workload; pgvector should not introduce a second, different one.

**Parameters.** `m = 16` / `ef_construction = 64` are pgvector's documented defaults and are the
right choice here. The repo's FAISS numbers (`m=32`, `efc=200`) are tuned for >10 000 vectors and
would cost needless build time and index memory on a corpus this small. Revisit and raise toward
the FAISS values only if `memory_chunks` passes ~10 000 rows — the same threshold the FAISS store
already uses.

**Query-time recall knob**, set per session or per transaction, not baked in:

```sql
SET LOCAL hnsw.ef_search = 40;   -- default 40; raise for recall, lower for latency
```

**Expected row count, and the honest caveat.** Today: **1 document, 1 chunk**. Realistically the
owner accumulates tens to low hundreds of notes over years — call it `10^1`–`10^3` chunks. At
that scale **PostgreSQL will correctly prefer a sequential scan and never touch the HNSW index**;
an exact scan over a thousand 768-dim vectors is sub-millisecond. The HNSW index is therefore
**forward-looking insurance, not a current performance need.** It is included because it costs
nothing to maintain at this size and retrofitting it later requires a blocking `CREATE INDEX`.
Do not expect `EXPLAIN` to show it in use, and do not "fix" that by disabling `seqscan`.

**Operator class: `vector_cosine_ops` (`<=>`).** pgvector's cosine operator normalises
internally, so it is correct **even if a writer forgets to normalise**. The alternative,
`vector_ip_ops` (`<#>`), is marginally cheaper and would give identical ordering *provided* every
vector is unit-length — and the existing write path does normalise via
`normalise_embedding` (`embedding_vector.py:34-49`, which returns a unit vector). Cosine is
chosen because it is self-correcting rather than dependent on that discipline. *If* profiling
later shows the normalisation cost matters, switching to `vector_ip_ops` is safe specifically
because the writer normalises — but that becomes a load-bearing invariant and must be asserted in
tests.

**Distance is 0-identical to today's semantics:** `fast_vector_store.py:91` uses `IndexFlatIP`
"for cosine similarity (with normalized vectors)" and `vector_store.py:106` converts similarity
to `distance = 1.0 - similarity`. pgvector's `<=>` returns `1 - cosine_similarity`, the same
number.

### 2.6 Hybrid retrieval query

```sql
WITH q AS (
    SELECT $1::vector(768) AS query_vec,     -- NULL when the embedding failed
           websearch_to_tsquery('english', $2) AS query_ts
),
vec AS (
    SELECT c.id, c.document_id, d.title, c.heading, c.content,
           ROW_NUMBER() OVER (ORDER BY c.embedding <=> q.query_vec) AS rank
    FROM memory_chunks c
    JOIN memory_documents d ON d.id = c.document_id
    CROSS JOIN q
    WHERE q.query_vec IS NOT NULL
      AND c.embedding IS NOT NULL
      AND d.always_loaded IS FALSE          -- the router is already in the prompt
    ORDER BY c.embedding <=> q.query_vec
    LIMIT $3                                 -- candidate pool, e.g. 20
),
fts AS (
    SELECT c.id, c.document_id, d.title, c.heading, c.content,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank_cd(c.tsv, q.query_ts) DESC
           ) AS rank
    FROM memory_chunks c
    JOIN memory_documents d ON d.id = c.document_id
    CROSS JOIN q
    WHERE c.tsv @@ q.query_ts
      AND d.always_loaded IS FALSE
    ORDER BY ts_rank_cd(c.tsv, q.query_ts) DESC
    LIMIT $3
)
SELECT id, document_id, title, heading, content,
       SUM(1.0 / (60 + rank)) AS rrf_score
FROM (SELECT * FROM vec UNION ALL SELECT * FROM fts) hits
GROUP BY id, document_id, title, heading, content
ORDER BY rrf_score DESC
LIMIT $4;                                    -- final N, e.g. 5
```

`k = 60` is the standard reciprocal-rank-fusion constant and matches the intent of the existing
`_reciprocal_rank_fusion` (`memory/db.py:95`). When `query_vec IS NULL` the `vec` CTE is empty and
this degrades to pure keyword ranking — the same fail-open-to-FTS behaviour
`search_conversation_memory` already has (`conversation.py:2005-2013`). The
`always_loaded IS FALSE` filter prevents paying twice for text already injected verbatim.

### 2.7 Retrieval budget

Today the prompt pays 1076 B for memory (16 %). Retrieved chunks must be capped, or a growing
vault will silently crowd out the conversation.

**Recommendation:** a new `memory_recall_budget_bytes` setting, default **1500** (~375 tokens),
enforced by the same whole-line-boundary discipline `_truncate_body` already uses
(`router.py:195`). The router keeps its own independent `budget_bytes` (6000). Worst case the
prompt pays ~6246 (router ceiling) + 1500 (recall) ≈ 7.7 KB ≈ 1.9 k tokens of memory.

*1500 is a judgement call, not a measurement.* It is deliberately below the router's 6000 so
that remembered notes never dominate, and it should be tuned against real notes.

---

## 3. Embedding choice

### 3.1 Reuse analysis — there is already an embedding stack; use it

**Do not build a second embedding client.** The repo already has a provider-aware embedding
stack, and the new layer must call into it rather than reimplementing HTTP.

**The four `utils/` modules — what they actually do:**

| Module | Lines | What it is | Keyed by |
|---|---|---|---|
| `utils/embedding_vector.py` | 49 | The dimension contract. `EMBEDDING_DIMENSION = 768` (`:31`) and `normalise_embedding(vector, dimension=None)` (`:34`), which rejects non-numeric, non-finite, zero-magnitude and wrong-width vectors and returns a unit `float32` vector. | n/a |
| `utils/vector_store.py` | 158 | `PythonVectorStore`: a pure-Python fallback. Holds `{summary_id: np.ndarray}` in memory, `search()` is an `O(n)` cosine loop (`:84-110`), vectors persisted as JSON in SQLite table `python_vector_store.vector_json`. `get_best_vector_store()` (`:146`) prefers FAISS. | `summary_id` |
| `utils/fast_vector_store.py` | 252 | `FAISSVectorStore`: the production path. Loads every row of SQLite table `faiss_vector_store.vector_blob` at construction (`_load_vectors`, `:43`), rebuilds the FAISS index in memory. `IndexFlatIP` for ≤10 000 vectors, `IndexHNSWFlat(dim, 32)` with `efConstruction=200`/`efSearch=50` above (`:96-118`). | `summary_id` |
| `utils/vector_persistence.py` | 36 | `persist_vector(db_path, backend, summary_id, value, source_text)`: one atomic `INSERT OR REPLACE` into `faiss_vector_store` **guarded by a staleness check** — the row is written only if the diary's current `summary || ' ' || topics` still equals the text that was embedded (`:22-28`). | `summary_id` |

**Every call site of those four modules is the diary, not the vault:**

- `memory/db.py:129-135` — `Database.__init__` constructs the store with
  `dimension=EMBEDDING_DIMENSION` and logs which backend it got.
- `memory/db.py:183`, `:205` — `search_hybrid` calls `self._python_vector_store.search(...)`.
- `memory/db.py:444-451` — `upsert_summary_embedding` writes through `normalise_embedding`, then
  `add_vector` or `add_summary_vector`.
- `memory/conversation.py:262`, `:552`, `:1833` — gate re-embedding on
  `cfg.embedding_model and db.has_vector_store`.
- `llm/gemini.py:47`, `:70` — import `EMBEDDING_DIMENSION` and pin Gemini's
  `outputDimensionality` to it.

So all four index **`conversation_summaries`**, addressed by `summary_id`. **Vault memory has
never touched them.** That is the whole of the overlap question, answered below.

**The single source of embeddings becomes:**

```python
# src/claire/memory/embedder.py  (new, thin — no new HTTP client)
from ..llm.factory import get_embedding_backend
from ..utils.embedding_vector import EMBEDDING_DIMENSION, normalise_embedding

def embed_for_memory(text: str, cfg, *, timeout_sec: float | None = None) -> list[float] | None:
    """The one function that turns memory text into a stored vector."""
    backend = get_embedding_backend(cfg)          # llm/factory.py:128
    raw = backend.embed(text, cfg.embedding_model,
                        timeout_sec=timeout_sec or cfg.llm_embedding_timeout_sec)
    if raw is None:
        return None
    try:
        vec = normalise_embedding(raw, EMBEDDING_DIMENSION)   # raises on bad width
    except ValueError:
        return None
    return vec.tolist()
```

This is the same call `conversation.py:74 _embed_text` makes, and it inherits for free:

- the **provider-aware model resolution** of §3.3 (so no model name is hardcoded),
- **Ollama's own failure text** (`ollama.py:358-362 embed_with_reason`, which turns
  `model "nomic-embed-text" not found, try pulling it first` into a runnable instruction),
- the Gemini branch (`llm/factory.py:128`) if the owner ever switches,
- and the dimension gate, because `normalise_embedding` raises on a width mismatch.

`_embed_text` itself stays where it is: it is the diary's entry point and also carries
`task_type` for Gemini's asymmetric retrieval (`conversation.py:86-92`). The two are thin enough
that a shared helper is optional; **what matters is that both bottom out in
`get_embedding_backend(cfg).embed(...)` and there is no third path.**

**Route confirmation (measured).** Because the live `config.json` sets
`llm_provider = "openai_compatible"` and omits `embedding_provider`, the effective embedding route
is **OmniRouter** (`http://127.0.0.1:20129/v1`), not ollama's own port. I verified this is
harmless: for identical input text, OmniRouter's `nomic-embed-text` response, ollama
`/api/embed`, and ollama `/api/embeddings` are **bit-identical** (`cos = 1.000000`, arrays equal),
all 768-dim, all already L2-normalised. OmniRouter proxies the same local model. There is
therefore **no cross-route vector-space hazard today** — but see risk R3.

### 3.2 Overlap analysis — keep, retire, or repurpose

| Component | Indexes | Verdict | Reason and blast radius |
|---|---|---|---|
| `utils/embedding_vector.py` | nothing (contract + validator) | **KEEP — becomes shared** | Both the diary and the new pgvector layer write `vector(768)`. This module is the one place that constant and the validator live. Blast radius of keeping: none. |
| `utils/fast_vector_store.py` | `conversation_summaries` via `summary_id` | **KEEP as-is** | It indexes the **diary**, not vault memory. `tests/test_diary_embedding_freshness.py`, `test_diary_embedding_failure.py`, `test_vector_store_isolation.py` and `test_vector_write_failure.py` all exercise this path. Retiring it means re-homing the diary onto pgvector — scope creep with real risk and no vault benefit. |
| `utils/vector_store.py` | same, pure-Python fallback | **KEEP as-is** | Fallback when `faiss-cpu` is absent (`get_best_vector_store`, `:146-158`). Zero cost to keep. |
| `utils/vector_persistence.py` | `faiss_vector_store` / `python_vector_store` | **KEEP as-is** | Implements the diary's staleness guard (`:22-28`) which has a dedicated test. Orthogonal to vault memory. |
| `memory/db.py` (SQLite `Database`) | diary, meals, graph, FTS, FAISS | **KEEP, unchanged** | Holds `conversation_summaries` (6), `memory_nodes` (4), `meals` (0), `summaries_fts` (6). Vault memory is not in it. **Do not migrate it as part of this work.** |
| `memory/notes.py` | vault files | **REPURPOSE** | Stops being a hot-path reader. Becomes the **import/export** layer: `parse_frontmatter`, `strip_frontmatter`, `extract_title`, `_iter_note_files`, `normalize_note_name` are reused verbatim by the importer and the exporter. `search_notes`/`list_notes`/`read_note` remain only as the export/verification surface and are **deleted from the panel's read path** (§7). |
| `memory/router.py` | `CLAIRE.md` | **REPURPOSE** | `_truncate_body`, `_budget_warning`, `_coerce_budget`, `MEMORY_HEADER` and the `render_for_prompt()` contract are all preserved; only the *source* of `frontmatter`/`body` changes from `Path.read_text()` to a Postgres row. The class keeps its name and property surface, so `system_prompt.py` barely moves. |
| `memory/obsidian.py` | vault layout | **REPURPOSE** | `describe_vault`/`ensure_vault` stop being runtime concerns. `validate_note_document` is still useful as an *export-time* integrity check. |

**Answer to "do they become redundant?":** **No.** They index the diary and chat history, which
this migration does not touch. The only genuinely vault-coupled code is `memory/notes.py`,
`memory/router.py`, and the panel handlers — and of those, `router.py`'s budget logic is
**preserved, not replaced**. Nothing in `utils/` is deleted by this work.

### 3.3 Where the model name comes from — honour `config.json`

`embedding_model` is **not** hardcoded in the new layer. Resolution, in `src/claire/config.py`:

| Step | Location | Behaviour |
|---|---|---|
| Default source | `config.py:668` | `"ollama_embed_model": "nomic-embed-text"` — the default is the *Ollama pull name* |
| Read from config | `config.py:989` | `ollama_embed_model = str(merged.get("ollama_embed_model"))` |
| Provider resolution | `config.py:1042-1058` | if effective provider is `gemini` → `GEMINI_DEFAULT_EMBED_MODEL`; if `openai_compatible` → `ollama_embed_model`; else → `ollama_embed_model` |
| Effective provider | `config.py:1046` | `_effective_embed_provider = embedding_provider or llm_provider` — **inherits the chat provider when unset** |
| Timeout | `config.py:693`, `:1338` | `llm_embedding_timeout_sec` default **5.0 s** |
| Config file on disk | `~/.config/claire/config.json` | live file has **no** `embedding_model` and **no** `embedding_provider` (verified) |

So today `cfg.embedding_model == "nomic-embed-text"` by default and the route is OmniRouter.
`embed_for_memory` reads `cfg.embedding_model` and never names a model. The importer records the
model and provider into `memory_chunks.embed_model` / `embed_provider` so the corpus is
self-describing.

### 3.4 Sync vs async: **sync psycopg 3**

**Recommendation: `psycopg` (v3) with `psycopg_pool.ConnectionPool`, called synchronously** from
the existing code path. Reasons, in order of weight:

1. **The runtime is synchronous.** `reply/engine.py` has 0 `async def`; the daemon entry is
   `daemon.py:1673 def main(...)`; background work is `threading.Thread` (`listener.py:3173`,
   `:3182`, `:3189`, `:3235`; `reply/memory_prefetch.py:64`). There is no event loop to await on.
   Introducing async DB access would require either a loop that does not exist or
   `asyncio.run` per call — both worse than a blocking driver on a thread.
2. **The existing embedding call is already blocking.** `OllamaBackend.embed_with_reason` uses
   `requests.post` (`llm/ollama.py:367`), synchronously, with a 5 s default timeout. A sync
   DB driver matches it; an async one would be the only async component in the memory path.
3. **`psycopg3` supports both**, so choosing sync now does not preclude `AsyncConnection` later
   behind the new module's seam.
4. `psycopg[binary] 3.3.6` was confirmed downloadable in this environment
   (`.venv/bin/pip download psycopg[binary] --no-deps` → `psycopg-3.3.6-py3-none-any.whl`).
   Neither `psycopg` nor `psycopg2` is installed today (verified).

**Connection management.** One module-level `ConnectionPool(min_size=1, max_size=4)` in
`memory/pg.py`, opened lazily on first use and **never** at import time (so a missing Postgres
cannot break `import claire`). The pool is safe for the threaded runtime. `row_factory` set to
`dict_row`. Vectors are passed as `pgvector`-style JSON strings or via
`psycopg.types.json`/`register_vector`; the simplest dependency-free path is to pass
`'[0.1,0.2,…]'::vector` text with an explicit `::vector(768)` cast, which avoids adding the
`pgvector` Python package. **Recommendation:** start dependency-free (text + cast); adopt
`pgvector.psycopg.register_vector` only if the text-encoding cost ever shows up.

### 3.5 Batching, failure handling, and "ollama is down"

**Batching (import only).** The import embeds many chunks. Batch through one HTTP call rather
than N: Ollama's `/api/embed` accepts an `input` **array** and returns `embeddings[]` (measured:
`n_embeddings` matches input count, 768 each). Note `llm/ollama.py:368` currently posts to the
**legacy singular** `/api/embeddings` with one `prompt`, which cannot batch. The importer should
therefore either (a) call `/api/embed` directly for the batch case, or (b) accept N sequential
calls. At the current corpus size (1 chunk) this is moot; for a real import of hundreds of notes,
batch in groups of ~32. This is the one place a small, deliberate extension to the ollama backend
is justified — add `embed_many(texts, model, timeout)` to `llm/backend.py`/`llm/ollama.py`
alongside the existing `embed()`, rather than a second client.

**Per-turn path (single query embedding).** One query, one `embed()` call.

**Failure handling — three distinct cases:**

| Case | Behaviour |
|---|---|
| Embedding fails or times out (ollama down, model missing) | `embed_for_memory` returns `None`. Retrieval **degrades to FTS-only** via the `query_vec IS NULL` branch of §2.6 — the same fail-open-to-keyword behaviour `search_conversation_memory` already has (`conversation.py:2005-2013`). The turn is answered normally with keyword recall and a `debug_log` line (`conversation.py:98-102` sets the precedent that this must be logged, never silent). |
| Postgres is down, `memory_backend=postgres` | **Hard failure, no stale fallback.** The memory section renders `MEMORY_UNAVAILABLE_MARKER` (`system_prompt.py:131`, `[memory-unavailable]`) — the marker already exists and the render path already tolerates an unavailable router (`system_prompt.py:239-249`). The turn still completes: Claire answers without standing memory rather than refusing. A loud `ERROR` log names the DSN host/db (never the password). The panel self-check gains a failing `memory-db` probe. **Deliberately no read of the archived vault** — after cutover the vault is stale by definition, and answering from silently-stale memory is worse than saying memory is unavailable. |
| Embedding *succeeds* but returns the wrong width | `normalise_embedding` raises → treated as case 1. Never written. |

The distinction matters: **a missing embedding degrades recall; a missing database degrades the
assistant's standing memory but not its ability to answer.** Neither may raise out of a turn.

**Latency, measured.** Warm `embed()` is **15 ms** (ollama `/api/embeddings`) to **48 ms**
(OmniRouter). Cold, with model load, **2.18 s**. The listener already warms the embed model at
startup behind a flag (`listener.py:3235` `warmup-embed`, gated on
`cfg.embedding_model` at `:3158`), so the steady-state cost is ~15–50 ms per turn. **But the
timeout is `llm_embedding_timeout_sec` = 5.0 s**, and the call is blocking: a hung ollama can
stall a turn by five seconds. **Recommendation:** clamp the per-turn embedding timeout to
**~1.0 s** (imports may keep 5 s+), and reuse the existing hot cache
(`conversation.py:1264 hot_cache_get` / `:1281 hot_cache_put`) to avoid re-embedding an identical
query within a conversation.

---

## 4. Migration and import plan

### 4.1 The one-shot import

Because Postgres is now the single source of truth, the import is a **one-time, idempotent**
operation that retires the files. It is implemented as:

```
python -m claire memory import --vault ~/.local/share/claire/vault [--dry-run]
```

Steps, per file:

1. Enumerate `CLAIRE.md` and `notes/*.md` using the existing `_iter_note_files` (`notes.py:466`),
   which already excludes subdirectories and symlinks pointing outside the vault.
2. Read text; `parse_frontmatter(text)` (`notes.py:248`) → `(meta, body)`.
3. `doc_kind` = `'router'` for `CLAIRE.md`, `'note'` otherwise. `name` = note stem via
   `normalize_note_name`/`Path.stem` (`notes.py:328`), or `'CLAIRE'`.
4. `title` = `extract_title(text, fallback=stem)` (`notes.py:305`) — the same title the panel
   already shows.
5. `content_sha256 = sha256(body.encode('utf-8'))`.
6. Upsert (§4.2), then chunk (§2.4) and embed (§3.1).
7. `--dry-run` prints the plan (files, bytes, chunk counts, tokens) and writes nothing.

**Frontmatter → columns mapping:**

| Frontmatter key | Column | Rule |
|---|---|---|
| *(all keys)* | `frontmatter jsonb` | every key preserved verbatim (values are strings — `parse_frontmatter` yields `Dict[str,str]`) |
| `always_loaded` | `always_loaded boolean` | `meta.get('always_loaded','').strip().lower() in ('true','yes','1')`; forced `true` for the router regardless |
| `budget_bytes` | `budget_bytes integer` | parsed by the existing `_coerce_budget` (`router.py:185`) so unusable values mean "default" exactly as today |
| `title` | `title text` | `meta['title']` if present, else `extract_title(...)` |
| `updated` | *(frontmatter only)* | kept in jsonb; also used to set `source_mtime` if the file mtime is unhelpful |
| `role` | *(frontmatter only)* | informational; `doc_kind` is derived from the filename, not this key |
| any unrecognised key | *(frontmatter only)* | never dropped — that is the point of storing jsonb |

### 4.2 Idempotency — re-running must not duplicate

Idempotency rests on **two unique keys**, and re-running is a no-op by construction:

- `memory_documents.source_path` is `UNIQUE` → the row is identified, never duplicated.
- `memory_chunks (document_id, chunk_index)` is `UNIQUE` → same.

Upsert logic:

```sql
INSERT INTO memory_documents
    (source_path, doc_kind, name, title, body, frontmatter,
     always_loaded, budget_bytes, content_sha256, source_mtime, source_bytes)
VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11)
ON CONFLICT (source_path) DO UPDATE SET
    doc_kind       = EXCLUDED.doc_kind,
    name           = EXCLUDED.name,
    title          = EXCLUDED.title,
    body           = EXCLUDED.body,
    frontmatter    = EXCLUDED.frontmatter,
    always_loaded  = EXCLUDED.always_loaded,
    budget_bytes   = EXCLUDED.budget_bytes,
    source_bytes   = EXCLUDED.source_bytes,
    updated_at     = now()
WHERE memory_documents.content_sha256 IS DISTINCT FROM EXCLUDED.content_sha256
   OR memory_documents.frontmatter    IS DISTINCT FROM EXCLUDED.frontmatter
RETURNING id, (xmax = 0) AS inserted;
```

- **`RETURNING` is empty when nothing changed.** That is the fast path: a second run over an
  unchanged vault touches no row, re-chunks nothing, re-embeds nothing. This is the property the
  owner asked for, and it is enforced by the database, not by bookkeeping in Python.
- `xmax = 0` distinguishes insert from update, for reporting.
- **Chunk-level re-embedding:** after a document update, chunk the new body; for each
  `(document_id, chunk_index)`, `INSERT … ON CONFLICT … DO UPDATE` and re-embed **only** where
  `memory_chunks.content_sha256 IS DISTINCT FROM EXCLUDED.content_sha256` *or* `embedding IS
  NULL`. Editing one paragraph re-embeds one chunk. Chunks beyond the new count are deleted
  (`DELETE FROM memory_chunks WHERE document_id = $1 AND chunk_index >= $2`).
- **Deletions are not inferred.** The vault is retired, so the import is not a sync. A `--prune`
  flag may delete documents whose `source_path` is absent from the vault, but it is **not** the
  default, because after retirement the files are an archive, not a source.
- **A dimension/model guard before embedding:** if `memory_chunks` already holds rows whose
  `embed_model` differs from the configured one, the importer **refuses** and requires an explicit
  `--reembed` rather than mixing incomparable vectors in one column
  (`embedding_vector.py:21-25` spells out exactly this hazard).

### 4.3 One-way export, never on the hot path

```
python -m claire memory export --out ~/Documents/claire-vault-export
```

Reconstructs `CLAIRE.md` and `notes/*.md` from Postgres, re-rendering frontmatter with the
existing `render_frontmatter` (`notes.py:750`), so Obsidian can still open the result and the
owner can still read and diff their memory as plain text. Intended for backup, review, and
Obsidian browsing. **It is manual, one-way, and never called by any turn path.** No watcher, no
`inotify`, no import-on-edit. This is the explicit rejection of two-way sync: two writers to one
memory is how a fact silently reverts.

Backups should be `pg_dump` (below), not the export, but the export is what makes the data
*legible* — worth having for the owner's confidence, and it keeps Obsidian in the picture as a
viewer.

---

## 5. Cutover strategy

Postgres becomes the single source of truth; the vault is retired. A deliberate restart is
authorised. The sequence is ordered so that **the old system remains the working one until the
last possible moment**, and so that the flip is a single env var.

### 5.1 Prerequisites (do these first; none of them affect the running assistant)

```bash
# 1. Confirm pg17 and the extension (verified during this review: PG 17.11, vector 0.8.7, 0 tables)
docker ps --filter name=claire-pg --format '{{.Image}} {{.Status}}'
docker exec claire-pg psql -U claire -d claire -tAc "select version(); select extversion from pg_extension where extname='vector';"

# 2. Back up the vault BEFORE anything else, and record its hash.
cp -a ~/.local/share/claire/vault ~/.local/share/claire/vault.backup-$(date +%F)
sha256sum ~/.local/share/claire/vault/CLAIRE.md   # expect 1955b0e6…16152

# 3. Back up the SQLite store too (it is NOT migrated, but it is adjacent and cheap).
sqlite3 ~/.local/share/claire/claire.db ".backup '$HOME/.local/share/claire/claire.db.bak-$(date +%F)'"

# 4. Install the driver and prove the DSN works from the venv.
.venv/bin/pip install 'psycopg[binary,pool]==3.3.*'
set -a; source ~/.config/claire/pg.env; set +a
.venv/bin/python -c "
import os, psycopg
with psycopg.connect(os.environ['CLAIRE_PG_DSN']) as c:
    print(c.execute('select version()').fetchone()[0])
"
```

### 5.2 **Wire the DSN into the services — required, and currently missing**

This is the step that silently breaks everything if skipped (see §0(b)): `pg.env` is 0600 and
correct, but **no systemd unit loads it**, and nothing in the repo references it.

Preferred: add a drop-in, which the installer never overwrites (the same reasoning already
documented in `claire.service.d/omniroute-ordering.conf`):

```bash
cat > ~/.config/systemd/user/claire.service.d/pg-env.conf <<'EOF'
[Service]
EnvironmentFile=-%h/.config/claire/pg.env
EOF
cp ~/.config/systemd/user/claire.service.d/pg-env.conf \
   ~/.config/systemd/user/claire-panel.service.d/pg-env.conf   # create the dir if needed
systemctl --user daemon-reload
```

Alternative, if the owner prefers one file: append the six `CLAIRE_PG_*` values to
`~/.config/claire/claire.env` (also 0600), which both units already load. Either way, **verify
from inside the unit's environment** rather than from an interactive shell:

```bash
systemctl --user show-environment >/dev/null   # sanity
env -i $(systemctl --user show-environment | grep CLAIRE_PG_ | tr '\n' ' ') \
  .venv/bin/python -c "import os; print(bool(os.environ.get('CLAIRE_PG_DSN')))"
```

### 5.3 Gated rollout steps

```bash
# Step 1 — schema. Idempotent; creates §2's DDL. Safe to re-run.
PYTHONPATH=src .venv/bin/python -m claire memory migrate
#   expect: schema_migrations has version 1; memory_documents and memory_chunks exist

# Step 2 — dry-run import. Writes NOTHING. Inspect the plan.
PYTHONPATH=src .venv/bin/python -m claire memory import --vault ~/.local/share/claire/vault --dry-run
#   expect today: 1 document ('CLAIRE.md', kind=router), 0 notes, 1 chunk, 804-byte body

# Step 3 — real import.
PYTHONPATH=src .venv/bin/python -m claire memory import --vault ~/.local/share/claire/vault

# Step 4 — VERIFY BEFORE FLIPPING. All of these must pass.
docker exec claire-pg psql -U claire -d claire -c "
  SELECT doc_kind, count(*) FROM memory_documents GROUP BY 1;
  SELECT count(*) AS chunks,
         count(*) FILTER (WHERE embedding IS NULL) AS unembedded,
         count(DISTINCT embed_model) AS models
  FROM memory_chunks;"
#   expect: router=1; chunks>=1; unembedded=0; models=1

# 4a. Vector round-trip: the router text must find itself.
docker exec claire-pg psql -U claire -d claire -c "
  SELECT id, heading, 1 - (embedding <=> (SELECT embedding FROM memory_chunks LIMIT 1)) AS cos
  FROM memory_chunks ORDER BY embedding <=> (SELECT embedding FROM memory_chunks LIMIT 1) LIMIT 3;"
#   expect: the same row at cos = 1.0

# 4b. Byte fidelity vs the file: body length must equal the measured 804.
docker exec claire-pg psql -U claire -d claire -tAc \
  "SELECT octet_length(body) FROM memory_documents WHERE doc_kind='router';"
#   expect: 804

# 4c. Re-run the import. It must be a NO-OP (zero rows returned by the upsert).
PYTHONPATH=src .venv/bin/python -m claire memory import --vault ~/.local/share/claire/vault
#   expect: "0 documents changed, 0 chunks embedded"

# 4d. Export and diff against the original — proves the round trip is lossless.
PYTHONPATH=src .venv/bin/python -m claire memory export --out /tmp/vault-roundtrip
diff -r ~/.local/share/claire/vault /tmp/vault-roundtrip   # expect only .obsidian/ to differ
```

### 5.4 The flip

```bash
# Step 5 — flip the flag (see §5.5 for the name and the default).
printf '\n# Memory backend: postgres (single source of truth) | vault (legacy)\nCLAIRE_MEMORY_BACKEND=postgres\n' \
  >> ~/.config/claire/claire.env

# Step 6 — deliberate restart, authorised for this cutover.
systemctl --user restart claire.service claire-panel.service
systemctl --user is-active claire.service claire-panel.service   # expect: active active

# Step 7 — live verification.
journalctl --user -u claire.service -n 200 --no-pager | grep -iE 'memory|pg|postgres|migrat'
#   expect: no [memory-unavailable], no traceback, a line naming the postgres backend

# 7a. The prompt still carries memory, now from Postgres.
PYTHONPATH=src .venv/bin/python -c "
from claire.config import load_settings
from claire.system_prompt import build_system_prompt, MEMORY_UNAVAILABLE_MARKER
s = build_system_prompt('Claire', load_default_router=True)
print('bytes:', len(s.encode()), 'unavailable-marker present:', MEMORY_UNAVAILABLE_MARKER in s)
print(s[s.find('## Memory'):][:300])"
#   expect: ~6705 bytes (unchanged), marker absent, the real CLAIRE.md text below the heading

# 7b. The functional win: a paraphrased query that keyword search CANNOT match.
#     'dentist' note text is 'Dr. Ruiz, Wednesdays at 16:00'; ask about 'teeth'.
PYTHONPATH=src .venv/bin/python -c "
from claire.config import load_settings
from claire.memory.pg_store import search_memory
cfg = load_settings()
print(search_memory('when am I seeing the teeth doctor?', cfg, limit=5))"
#   expect: the dentist chunk, which search_notes() could never return

# 7c. Panel: open the memory page and confirm the router renders and search works.
```

### 5.5 The flag, and what the default must be

| | |
|---|---|
| **Env var** | `CLAIRE_MEMORY_BACKEND` |
| **Values** | `vault` (legacy Markdown) \| `postgres` (single source of truth) |
| **Default in code** | **`vault`** |
| **Where it lives** | read via the existing runtime-env mechanism — `config.py:889 runtime_env_file()` → `~/.config/claire/claire.env`, loaded by `load_runtime_env()` (`config.py:933`) and by `EnvironmentFile=-%h/.config/claire/claire.env` in both units |

**The default must remain `vault` until the owner flips it.** Concretely, the resolution is
`os.environ.get("CLAIRE_MEMORY_BACKEND", "vault")` — an unknown value must also mean `vault`
(fail safe toward the known-good system, and log the unrecognised value). A missing or malformed
flag must never silently select an unverified database.

### 5.6 Archive the vault (only after 5.4 passes)

```bash
# Rename, never delete. The .md files remain the owner's readable record.
mv ~/.local/share/claire/vault ~/.local/share/claire/vault.retired-$(date +%F)
# Keep the backup from 5.1 too — two independent copies.
ls -la ~/.local/share/claire/ | grep vault
```

After this, **nothing reads or writes `*.md` memory.** `notes.py`'s readers remain only for
`memory export` and for the verification commands above, which take an explicit path and are
never invoked by a turn.

### 5.7 Rollback — exact procedure

**Rollback is a flag flip plus the archived vault, and it is valid only until the first write
lands in Postgres.** State that plainly: the moment Claire or the panel writes a note through the
new backend, the archived vault no longer describes her memory, and a bare revert would silently
lose that fact.

**Window A — no writes have landed (immediately after 5.4): a clean, lossless revert.**

```bash
# 1. Flip back.
sed -i 's/^CLAIRE_MEMORY_BACKEND=postgres/CLAIRE_MEMORY_BACKEND=vault/' ~/.config/claire/claire.env
grep CLAIRE_MEMORY_BACKEND ~/.config/claire/claire.env    # expect: =vault

# 2. Restore the vault to its original path.
mv ~/.local/share/claire/vault.retired-$(date +%F) ~/.local/share/claire/vault
sha256sum ~/.local/share/claire/vault/CLAIRE.md   # must equal 1955b0e6…16152

# 3. Restart and verify.
systemctl --user restart claire.service claire-panel.service
PYTHONPATH=src .venv/bin/python -c "
from claire.memory.router import load_router
r = load_router(); print(r.path, r.size_bytes, r.over_budget)"
#   expect: …/vault/CLAIRE.md 804 False
```

Postgres and its data are left **untouched** by a rollback — no `DROP`, no down-migration. A
failed cutover must not also destroy the imported data.

**Window B — writes have landed: export first, then roll back.**

```bash
# 1. Regenerate the vault from Postgres so nothing is lost.
PYTHONPATH=src .venv/bin/python -m claire memory export --out ~/.local/share/claire/vault-restored
# 2. Flip the flag and point the vault path at the restored tree.
sed -i 's/^CLAIRE_MEMORY_BACKEND=postgres/CLAIRE_MEMORY_BACKEND=vault/' ~/.config/claire/claire.env
echo 'CLAIRE_VAULT_DIR='"$HOME"'/.local/share/claire/vault-restored' >> ~/.config/claire/claire.env
systemctl --user restart claire.service claire-panel.service
```

**A whole-machine revert is also available and cheap**, because Postgres holds nothing the vault
does not:

```bash
docker stop claire-pg && docker rm claire-pg      # volume claire-pgdata17 is NOT removed
# with the flag at 'vault', Claire runs exactly as before this project.
```

**Rollback is not silent.** Any failure that triggers a revert must be logged with the DSN
host/database (never the password) and the failing operation, and the panel self-check must show
the `memory-db` probe failing — so the owner sees *why* before they see the flag.

---

## 6. Risks and open questions

| # | Risk / question | Impact | Likelihood | Mitigation / decision |
|---|---|---|---|---|
| R1 | **`pg.env` is not loaded by the service units.** Verified: no repo reference, no `EnvironmentFile` entry; both units load only `claire.env`. | Postgres memory works when run by hand and **fails under systemd** — a confusing, half-broken state. | **Certain** if §5.2 is skipped | Add the `pg-env.conf` drop-in to both units (§5.2) and verify from the unit's environment, not an interactive shell. **Must be done before the flip.** |
| R2 | **Postgres down while `memory_backend=postgres`.** | Standing memory disappears from the prompt; semantic recall is unavailable. The turn still answers. | Low (local container, `restart` policy, `Up healthy`) | Render `MEMORY_UNAVAILABLE_MARKER` (`system_prompt.py:131`), log loudly, keep answering, fail the panel `memory-db` probe. **No stale-vault fallback** — after cutover the vault is stale by definition and silent staleness is worse than a named gap. Pool `connect_timeout` ~2 s so a dead DB costs at most ~2 s once, not per call. |
| R3 | **Embedding-model or provider change silently mixes vector spaces.** `embedding_provider` inherits `llm_provider` (`config.py:1046`), so changing the *chat* provider can change the *embedding* route. A Gemini embedding model is 768-dim but **not comparable** to nomic (`embedding_vector.py:21-25`). | Semantic recall returns plausible-looking nonsense, with no error. | Medium over time | Record `embed_model` + `embed_provider` per chunk (§2.4); the importer refuses to mix models without `--reembed` (§4.2). Verified today: OmniRouter and ollama are bit-identical, so the current route is safe. Consider pinning `embedding_provider: "ollama"` in `config.json` to decouple embeddings from the chat provider. |
| R4 | **Embedding latency per turn vs today.** Today the hot path embeds **nothing** — it reads a file. The new path adds one embedding call. | Warm: 15 ms (ollama) / 48 ms (OmniRouter) — negligible. Cold: 2.18 s. **Timeout is 5 s and the call blocks.** | Cold case is likely on the first turn after a restart; a hung ollama blocks for the full timeout | The listener already warms the model (`listener.py:3235`). Clamp the per-turn timeout to ~1 s; reuse the hot cache (`conversation.py:1264/:1281`) for repeat queries. |
| R5 | **Should `always_loaded` stay a file/flag at all?** Today it is decorative — no code branches on it (§1.2). The real contract is the filename plus `engine.py:1735`. | Low now; it is a latent trap if someone adds a second file and marks it `always_loaded` expecting it to be injected. | Low | Migrate it to a real `boolean` column (§2.3) and have the hot path select `WHERE always_loaded` with a unique partial index enforcing at most one router. Either make it load-bearing or delete the key — do not leave it decorative. |
| R6 | **Prompt growth.** Retrieved chunks add to a prompt already paying 1076 B (16 %) for memory. | Unbounded growth would crowd out conversation and raise cost per turn. | Medium once notes exist | Independent `memory_recall_budget_bytes` default 1500, enforced at whole-line boundaries (`router.py:195`). Hard ceiling ≈ 7.7 KB of memory. **The 1500 value is a recommendation, not a measurement** — tune against real notes. |
| R7 | **`to_tsvector('english', …)` assumes English prose.** | Non-English memory would be stemmed oddly, weakening FTS recall (the vector half would still work). | Low (live vault is English) | Ship `'english'`; if non-English memory appears, switch `tsv` to `'simple'` (a migration + reindex). Flagged, not resolved. |
| R8 | **Chunking parameters are untested against real data.** The vault has **zero notes**, so the 1500-char / 15 %-overlap rule has never seen a real note. | Retrieval quality may need tuning. | High (it is unvalidated by definition) | Ship the rule behind the recall budget; revisit once real notes exist. The store is small enough that re-chunking + re-embedding the whole corpus is seconds. |
| R9 | **HNSW index will not be used at the current row count.** PostgreSQL will prefer a sequential scan for ~1–1000 rows. | None functionally; a reviewer may mistake it for a misconfiguration. | Certain | Documented in §2.5. Keep `enable_seqscan` at its default; do not force the index. |
| R10 | **Backup story changes.** Today memory is a file the owner can copy; now it is a Docker volume (`claire-pgdata17`). | Loss of the volume loses memory. | Low | `pg_dump` on a schedule + the one-way `memory export` for human-readable copies. Note the volume name **changed** from `claire-pgdata` to `claire-pgdata17` in the pg16→pg17 swap — verify the name before any backup script is written against it. |
| R11 | **Losing Obsidian as an editor.** The owner currently edits `CLAIRE.md` in Obsidian/Vim and behaviour changes with no restart (`router.py:9-13`). | A live-editing workflow stops working. | Medium — this is a real workflow regression | `memory export` keeps Obsidian as a *viewer*; note editing moves to the panel. If live file editing is a hard requirement, the honest answer is that it conflicts with "Postgres is the source of truth" and needs a decision. **Open question for the owner.** |
| R12 | **psycopg is not installed** (verified: neither `psycopg` nor `psycopg2` in `.venv`), and `requirements.txt`/`pyproject.toml` do not list it. | The new code cannot import under systemd, which uses `.venv`. | Certain if forgotten | `pip install 'psycopg[binary,pool]==3.3.*'` **and** add it to `requirements.txt`/`pyproject.toml` (§5.1). `psycopg[binary] 3.3.6` confirmed downloadable here. |
| R13 | **Secrets in the DSN.** `pg.env` embeds the password in `CLAIRE_PG_DSN`, and `config.json` already contains a live `llm_api_key` in cleartext. | Password disclosure in logs or the repo. | Low if disciplined | `pg.env` is 0600 and outside the repo (verified). Log only host/database, never the DSN. **Never** commit `pg.env`; the new code must not print `CLAIRE_PG_DSN`. |
| R14 | **Not verified: whether the owner wants note *writing* exposed to the model.** Today no LLM tool writes vault memory (§1.3), and the panel is the only writer. | Scope decision that changes the tool surface and the risk profile (a hallucinating model editing its own standing memory). | n/a | §7 assumes **read-only semantic recall for the model**, writes via panel (and later an explicit, confirmed tool). **Open question.** |
| R15 | **Not verified: `pg_dump`/volume backup cadence, retention, and whether a restore has ever been rehearsed.** | An untested backup is not a backup. | Medium | Out of scope for this document; recommend a rehearsed restore into `claire_test` (`CLAIRE_PG_TEST_DB`, which exists in `pg.env`) before the flag flips. |
| R16 | **Not verified: OmniRouter's behaviour if ollama is unreachable.** OmniRouter proxies the same local model (verified bit-identical), so if ollama is down OmniRouter's embedding route should fail too — but I did not test that failure. | If OmniRouter instead falls back to a *different* embedding model, vectors would be incomparable — the R3 failure mode, triggered by an outage. | Low, but severe if true | Pin `embedding_provider: "ollama"` to remove the dependency, **or** test the outage explicitly before relying on it. Flagged as unverified. |

---

## 7. Exact call sites to change

Ordered by criticality. Everything else in the repo is untouched.

| # | File:line | Today | Change |
|---|---|---|---|
| 1 | `system_prompt.py:172` `load_router_from_vault()` | `load_router()` reads `CLAIRE.md` from disk | Branch on `memory_backend`: `postgres` → build a `MemoryRouter`-shaped object from the `memory_documents` router row; `vault` → today's path. **The `budget_bytes` / `size_bytes` / `over_budget` / `render_for_prompt()` contract and all four markers stay identical**, so `build_memory_section` (`:233`) is unchanged. |
| 2 | `system_prompt.py:233-259` `build_memory_section` / `:261 _resolve_memory_section` | renders router, emits `MEMORY_UNAVAILABLE_MARKER` on failure | **Add** a bounded semantic-recall block from `search_memory()`; keep every existing marker and the `load_default_router=None` "read only if a vault exists" rule (`:271-287`). |
| 3 | `reply/engine.py:1735` | `build_system_prompt(_assistant_name, load_default_router=True)` | **No signature change.** Optionally pass the redacted query so recall is topic-aware; keep the call site working unchanged if not. |
| 4 | **new** `memory/pg.py` | — | Lazy `ConnectionPool`, DSN resolution from `CLAIRE_PG_DSN`, `apply_migrations()` against `schema_migrations`. Must not connect at import time. |
| 5 | **new** `memory/pg_store.py` | — | `document_upsert()`, `chunk_upsert()`, `search_memory()` (§2.6), `router_document()`. The single Postgres seam. |
| 6 | **new** `memory/embedder.py` | — | `embed_for_memory()` (§3.1) — the one embedding entry point for memory. Delegates to `llm/factory.py:128`. |
| 7 | **new** `memory/importer.py` / `exporter.py` | — | One-shot import (§4.1–4.2) and one-way export (§4.3). Reuses `notes.parse_frontmatter`, `strip_frontmatter`, `extract_title`, `_iter_note_files`, `render_frontmatter`, `normalize_note_name`. |
| 8 | `config.py` (~`:130-165`, `:660-700`, `:1330-1370`) | settings dataclass + defaults + assembly | Add `memory_backend` (default `"vault"`), `pg_dsn`, `memory_recall_budget_bytes` (1500). Read `CLAIRE_MEMORY_BACKEND` via the existing env mechanism. |
| 9 | `panel/server.py:1984, 2019, 2070, 2083, 2147` | panel note reads via `notes_module` | Point at `pg_store.search_memory` / document reads. The panel stops touching `.md` entirely. |
| 10 | `panel/server.py:2207 save_note`, `:2248 delete_note` | write files via `notes.create_note` | Write `memory_documents` + re-chunk + embed. Keep the refuse-by-default `PanelStateError` contract at `:1412-1425`. |
| 11 | `panel/server.py:1726-1728`, `:2227` | router path display, `ensure_vault` scaffolding | Router path comes from the DB row; `ensure_vault` becomes export-time only. |
| 12 | `platform/service.py:566 probe_vault` | "is the vault present and writable?" | Add `probe_memory_db` (connect + `select extversion from pg_extension where extname='vector'` + `max(version) from schema_migrations` + unembedded-chunk count). Keep `probe_vault` for the `vault` backend. |
| 13 | `memory/notes.py`, `memory/router.py`, `memory/obsidian.py` | vault readers/writers | **Not deleted.** `router.py` keeps its budget/render logic (source swapped); `notes.py` and `obsidian.py` become the import/export/validation layer. `search_notes` leaves the panel's read path. |
| 14 | `requirements.txt`, `pyproject.toml` | no Postgres driver | Add `psycopg[binary,pool]` (pin `3.3.*`). |
| 15 | **unchanged by design** | `memory/db.py`, `utils/vector_store.py`, `utils/fast_vector_store.py`, `utils/vector_persistence.py`, `utils/embedding_vector.py`, `memory/conversation.py`, `reply/memory_prefetch.py` | The diary/graph/FAISS stack and the dimension contract are untouched (§3.2). |

### Open questions for the owner

1. **Note editing UX (R11).** Is losing live file-editing in Obsidian acceptable? If not, the
   design needs a read-only file mirror written on a timer — which reintroduces a second copy and
   must be strictly one-way.
2. **Should the model be able to write memory (R14)?** Today it cannot. Giving it a write tool
   means it can edit its own standing memory, which is materially riskier than reading it.
3. **`memory_recall_budget_bytes` (R6)** and the 1500-char chunk size (R8) are recommendations,
   not measurements — they need real notes to tune.
4. **Backup cadence and a rehearsed restore (R15)** into `claire_test` before the flag flips.
5. **Pin `embedding_provider: "ollama"` (R3/R16)** to decouple embeddings from the chat provider,
   and to remove the untested OmniRouter-outage behaviour from the recall path.

---

## Appendix A — Verification performed for this document

The schema and the two non-trivial queries in §2 were **executed**, not merely written. All of it
ran against the test database `claire_test` (from `CLAIRE_PG_TEST_DSN` in
`~/.config/claire/pg.env`); the test objects were dropped afterwards and both `claire` and
`claire_test` are back to **zero tables** in `public`. The production database was never written.

| Check | Command / method | Result |
|---|---|---|
| DDL executes | §2 DDL run under `psql -v ON_ERROR_STOP=1` inside a transaction | `BEGIN … CREATE TABLE ×3, CREATE INDEX ×5 … COMMIT` — all objects created |
| HNSW index is real | `select indexdef from pg_indexes` | `USING hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='64')` |
| Extension available | `select amname from pg_am` | `hnsw`, `ivfflat` both present |
| `parse_frontmatter` on the live file | `notes.parse_frontmatter(CLAIRE.md)` | `{title, role, always_loaded, budget_bytes, updated}` — 5 keys, matches §4.1 |
| **Body byte count** | `len(body.encode())` | **804** — exactly the `MemoryRouter.size_bytes` measured in §1.1. The frontmatter→column mapping preserves the budget's meaning. |
| Idempotent import, run 1 | §4.2 upsert | `[(1, True)]` — one row inserted |
| **Idempotent import, run 2** | §4.2 upsert, identical input | **`[]`** — zero rows returned: the second run is a true no-op |
| Frontmatter-only edit | bump `updated:`, body unchanged | `[(1, False)]` — document updated, body **not** re-embedded (the `content_sha256` guard works) |
| Real embedding stored | ollama `/api/embed`, encoded as `::vector(768)` text | accepted; `vector_dims(embedding)` → `768` |
| Vector round-trip | `1 - (embedding <=> (select embedding …))` | **cos = 1.000000**, dims 768 — the row finds itself perfectly |
| Hybrid query (§2.6) with a query vector | real query embedding | 1 row, `rrf = 0.01639…` |
| **FTS-only degradation** (§2.6, `query_vec IS NULL`) | same query, NULL vector | 1 row, same score — the ollama-down path returns results instead of erroring |
| Provenance columns | `group by embed_model, embed_provider` | `('nomic-embed-text','openai_compatible')` — the live route is recorded, making R3 detectable |
| Cross-route vectors | same text on OmniRouter, `/api/embed`, `/api/embeddings` | bit-identical, `cos = 1.000000` (see §0) |

**Not verified, and flagged as such in §6:** the pg17 container swap's `pg_dump`/restore path
(R15), OmniRouter's behaviour when ollama is unreachable (R16), retrieval quality against real
notes (R8 — there are none), and the chunk-size/recall-budget numbers (R6/R8, recommendations
only).
