# Long-term memory

**Authoritative specs:** [`db.spec.md`](../../src/claire/memory/db.spec.md)
(the diary store, FTS5 and hybrid retrieval),
[`summariser.spec.md`](../../src/claire/memory/summariser.spec.md)
(diary generation and the deflection/topic sweeps),
[`graph.spec.md`](../../src/claire/memory/graph.spec.md) (the knowledge graph),
[`router.spec.md`](../../src/claire/memory/router.spec.md) (the always-loaded
router contract) and
[`recall_gate.spec.md`](../../src/claire/memory/recall_gate.spec.md)
(the skip heuristic). The operational runbook is
[`docs/PGVECTOR.md`](../PGVECTOR.md) and the conceptual overview is
[`docs/MEMORY.md`](../MEMORY.md). Read this when you are changing what Claire
remembers, where it is stored, what is recalled into a reply, or the memory
backend.

## Responsibility

Long-term memory is four stores with four different lifetimes. The router and the
notes live in PostgreSQL with pgvector; the diary and the knowledge graph live in
SQLite. None of them is a conversation log: the live conversation lives in
`DialogueMemory` in the daemon process only, and long-term memory is what
survives it.

| Store | Medium | Written by | Recalled into a reply |
|---|---|---|---|
| **Diary** — one row per `(date_utc, source_app)` | `conversation_summaries` in SQLite, with FTS5 and optional vector index | the background diary flush, after the short-term window closes | keyword/vector search for the current query |
| **Knowledge graph** — self-organising node tree | `memory_nodes` in the same SQLite file | fact extraction after a successful diary write | the always-on *warm profile* plus query-driven node search |
| **Router** — one always-loaded document | the `memory_documents` row with `doc_kind = 'router'` in PostgreSQL + pgvector | a human, via the panel's Memory page, or the importer | re-read from Postgres into the system prompt on **every** request |
| **Notes** — one document per fact | `memory_documents` rows with `doc_kind = 'note'`, embedded in `memory_chunks` | a human, via the panel's Memory page, or the importer | hybrid vector + full-text search; never auto-loaded |

The diary and the graph remain the only stores the model writes indirectly
(through summarisation and extraction, never through a tool call). The Postgres
store is never written by the model at all. The router plus the graph's warm
profile are re-assembled into the prompt on every turn, so correcting the router
corrects behaviour without a restart
(`system_prompt.py:391`, `engine.py:1735`). Which store the router and notes are
read from is decided by `_memory_backend()` (`system_prompt.py:176`):
`CLAIRE_MEMORY_BACKEND` in the environment wins, then `memory_backend` in
`config.json`, and an unrecognised value fails safe to `postgres`.

Nothing in a normal install reads the Markdown vault. The vault path is selected
only by setting the backend explicitly to `vault`; the vault format is what the
one-way export produces, and a normal install keeps no `CLAIRE.md` file on disk.

## Key modules

| Module | Owns | Anchors |
|---|---|---|
| `src/claire/memory/pg.py` | the Postgres store: schema, a lazy `ConnectionPool`, DSN resolution, document upsert, router and document reads, counts, and hybrid search fusing vector and full-text results | `EMBEDDING_DIM` `:32`, `SCHEMA_VERSION` `:33`, `resolve_dsn` `:155`, `get_pool` `:240`, `health` `:298`, `bootstrap_schema` `:316`, `Chunk` `:367`, `Document` `:387`, `upsert_document` `:433`, `get_document` `:587`, `get_router` `:600`, `list_documents` `:612`, `delete_document` `:624`, `count_documents` `:637`, `count_chunks` `:644`, `semantic_search` `:664`, `keyword_search` `:701`, `search` `:727` |
| `src/claire/memory/embedder.py` | the single embedding entry point for memory: `nomic-embed-text` at 768 dimensions, with a clamped per-turn timeout because a cold embedding backend is far slower than a warm one | `DEFAULT_TIMEOUT_SEC` `:27`, `EMBEDDING_DIM` `:30`, `model_name` `:65`, `provider_name` `:71`, `embed_text` `:84`, `embed_document` `:137`, `embed_query` `:148` |
| `src/claire/memory/pg_router.py` | the Postgres-backed router: a `MemoryRouter` subclass that overrides only `reload()`, so budget, line-boundary truncation and the over-budget warning stay identical to the file router | `ROUTER_SENTINEL_PATH` `:28`, `PostgresMemoryRouter` `:31`, `reload` `:55`, `save` `:69`, `router_exists` `:85`, `load_router_from_postgres` `:93` |
| `src/claire/memory/migrate_md_to_pg.py` | the one-shot idempotent importer and the one-way Markdown exporter; CLI `python -m claire.memory.migrate_md_to_pg` with `--dry-run`, `--vault`, `--dsn`, `--export`, `--no-bootstrap` and `--json`, reporting `created`/`updated`/`unchanged`/`skipped` | `MAX_SECTION_CHARS` `:33`, `ROUTER_FILENAME` `:37`, `ImportedDoc` `:41`, `ImportReport` `:56`, `import_vault` `:214`, `export_vault` `:324`, `main` `:352` |
| `src/claire/memory/db.py` | SQLite schema, FTS5 triggers, optional sqlite-vss/FAISS/Python vector index, weighted reciprocal rank fusion | `_SCHEMA_SQL` `:10`, `conversation_summaries` `:37`, `summaries_fts` `:47`, FTS triggers `:56`, `_VSS_SCHEMA_SQL` `:68`, `_reciprocal_rank_fusion` `:95`, `Database.__init__` `:110`, `has_vector_store` `:152`, `_init_schema` `:156`, FTS rebuild marker `:167`, `search_hybrid` `:177`, `upsert_conversation_summary` `:317`, `upsert_summary_embedding` `:409`, `close` `:456` |
| `src/claire/memory/conversation.py` | short-term `DialogueMemory`, the diary summariser, history budgeting, tool carryover, keyword/vector search, the deflection and topic sweeps | `_DIARY_SUMMARY_CHUNK_LIMIT` `:22`, `_embed_text` `:74`, `_rewrite_diary_summary` `:149`, `rewrite_all_diary_summaries` `:226`, `optimise_diary_topics` `:462`, `is_tool_message` `:676`, `bound_conversation_history` `:857`, `DialogueMemory` `:963`, `RECENT_WINDOW_SEC` `:1045`, `add_message` `:1074`, `record_tool_turn` `:1187`, `HOT_CACHE_MAX_ENTRIES` `:1262`, `hot_cache_put` `:1281`, `clear_hot_cache` `:1294`, `hot_cache_epoch` `:1306`, `get_recent_turns_with_tools` `:1339`, `get_pending_chunks_with_snapshot` `:1439`, `should_update_diary` `:1473`, `mark_saved_up_to` `:1506`, `generate_conversation_summary` `:1546`, `update_daily_conversation_summary` `:1711`, `search_conversation_memory_by_keywords` `:1854`, `search_conversation_memory` `:1963`, `update_diary_from_dialogue_memory` `:2128` |
| `src/claire/reply/memory_prefetch.py` | `MemoryExtractorPrefetch` — the speculative memory-extractor call the reply engine starts before the chat turn, and the epoch guard that stops an abandoned call repopulating a later conversation's hot cache | `MemoryExtractorPrefetch` `:44`, `start` `:85`, `_publish` `:107`, `result` `:129`, `_read_epoch` `:161`, `start_memory_extractor_prefetch` `:179` |
| `src/claire/memory/graph.py` | node schema, fixed branches, traversal, decay, mutation listeners | `register_graph_mutation_listener` `:38`, `SPLIT_THRESHOLD` `:89`, `FIXED_BRANCHES` `:124`, `FIXED_BRANCH_IDS` `:148`, `_GRAPH_SCHEMA_SQL` `:216`, `GraphMemoryStore` `:241`, `_ensure_root` `:268`, `migrate_legacy_shape` `:308`, `append_to_node` `:552`, `touch_node` `:568`, `get_recent_nodes` `:582`, `get_top_nodes` `:594`, `get_ancestors` `:642`, `search_nodes` `:684`, `get_graph_data` `:754` |
| `src/claire/memory/graph_ops.py` | extraction, semantic review, placement, merge, split, warm profile | `_review_graph_facts` `:83`, `extract_graph_memories` `:120`, unknown-branch fallback `:313`, `merge_node_data` `:617`, `auto_split_node` `:767`, `update_graph_from_dialogue` `:897`, automatic merge `:1028`, `consolidate_all_populated_nodes` `:1105`, `build_warm_profile` `:1207`, `format_warm_profile_block` `:1235` |
| `src/claire/memory/notes.py` | the **legacy vault path only**: vault path resolution, note frontmatter, note read/write/search, dangling-pointer report — no PyYAML | `VAULT_ENV_VAR` `:81`, `ROUTER_FILENAME` `:90`, `ROUTER_BUDGET_BYTES` `:93`, `resolve_vault_dir` `:132`, config-key step `:150`, `resolve_notes_dir` `:216`, `resolve_router_path` `:229`, `parse_frontmatter` `:248`, `normalize_note_name` `:328`, `list_notes` `:501`, `read_note` `:515`, `search_notes` `:585`, `unreferenced_notes` `:688`, `create_note` `:761`, `ensure_notes_dir` `:838`, `update_note` `:880`, `delete_note` `:938` |
| `src/claire/memory/router.py` | the file-backed router — legacy path: reading, byte-accounting, truncating and rendering `CLAIRE.md`; template creation. `PostgresMemoryRouter` inherits its render path | `MEMORY_HEADER` `:82`, `router_template` `:115`, `_truncate_body` `:195`, `MemoryRouter` `:221`, `budget_bytes` `:242`, `reload` `:266`, `render_for_prompt` `:282`, `ensure_router` `:307`, `load_router` `:348` |
| `src/claire/memory/recall_gate.py` | deterministic pre-flight check that can skip enrichment | `should_recall` `:50`, `min_coverage` `:54`, coverage computation `:83` |
| `src/claire/memory/obsidian.py` | Obsidian-compatible **vault shape and export format** — `.obsidian/`, wiki links, document validation, conventions — not a store. Library-only from the reply path's point of view; wired into the panel's vault browser | `VaultLayout` `:99`, `OBSIDIAN_DIRNAME` `:82`, `describe_vault` `:132`, `is_obsidian_vault` `:151`, `ensure_vault` `:157`, `wiki_link` `:189`, `extract_wiki_links` `:209`, `link_targets_in_text` `:219`, `resolve_wiki_link` `:239`, `validate_note_document` `:260`, `vault_conventions` `:331` |

`src/claire/memory/__init__.py` is empty: the package deliberately re-exports
nothing, so every caller imports the submodule it needs (for example
`from claire.memory import notes as notes_module`). There is no aggregate
`memory` API to extend.

The vault modules still ship and still work when the backend is explicitly set to
`vault`, so a rollback is a flag flip. `notes.py`, `obsidian.py` and the
file-backed path through `router.py` are reached only in that mode; nothing on
the default Postgres reply path reads a vault file.

Two modules outside `memory/` finish the picture. `src/claire/system_prompt.py`
renders the router into the prompt (`MEMORY_UNAVAILABLE_MARKER` `:132`,
`MEMORY_EMPTY_MARKER` `:133`, `MEMORY_OVER_BUDGET_MARKER` `:134`,
`_memory_backend` `:176`, `resolve_default_router_path` `:166`,
`load_router_from_vault` `:204`, `_memory_budget_bytes` `:227`,
`_render_router` `:238`, `build_memory_section` `:273`,
`_resolve_memory_section` `:301`, `build_system_prompt` `:391`); in `postgres`
mode `load_router_from_vault` delegates to
`pg_router.load_router_from_postgres`, and `_resolve_memory_section` treats the
router row as "provisioned". `src/claire/reply/engine.py` decides what is
recalled (`:1310`–`:1329` recall decision and gate, `:1354` warm profile,
`:1402` enrichment source, `:1423` extractor, `:1488` diary, `:1549` graph,
`:1598` digest, `:1735` prompt assembly, `:1735`–`:1838` prompt injection).

## Data and control flow

### What is written, in what order

**1. The short-term window fills.** `DialogueMemory.add_message`
(`conversation.py:1074`) appends `(timestamp, role, content)` under a lock and
assigns a strictly monotonic timestamp. `RECENT_WINDOW_SEC` and
`MAX_UNSAVED_AGE_SEC` are both the configured `dialogue_memory_timeout`
(`:1045`–`:1046`); `bound_conversation_history` (`:857`) caps what is *sent* to
the model (`dialogue_memory_max_tokens` / `_max_messages`) without capping what
is stored. Tool messages from the current conversation are re-recorded by
`record_tool_turn` (`:1187`) and re-injected by `get_recent_turns_with_tools`
(`:1339`).

**2. The flush trigger fires.** `should_update_diary` (`:1473`) returns true
when there are unsaved messages and either the user has been inactive for the
window or the oldest unsaved message has reached `MAX_UNSAVED_AGE_SEC`. The
daemon polls this every 60 s while it can take the chat-query lock
(`daemon.py:1998`–`:2004`, and again on the voice thread at `:2009`–`:2015`) and
forces a final flush at shutdown (`daemon.py:2057`). `_check_and_update_diary`
(`daemon.py:1068`) resolves `source_app` to `"stdin"` or `"voice"` (`:1132`).

**3. Pending work is snapshotted.** `update_diary_from_dialogue_memory`
(`conversation.py:2128`) takes `_diary_flush_lock` non-blocking, so flushes on
one `DialogueMemory` never overlap (`:2159`), then captures the pending chunks
*and* the snapshot timestamp atomically
(`_get_pending_diary_work` / `get_pending_chunks_with_snapshot`, `:2173`,
`:1439`). Messages arriving during summarisation stay unsaved because the
watermark is the captured snapshot, not wall-clock now.

**4. The summary is generated in bounded batches.**
`update_daily_conversation_summary` (`:1711`) redacts every chunk first
(`:1739`), reads today's existing row as the previous summary (`:1748`), and
runs `generate_conversation_summary` (`:1546`) over batches of at most
`_DIARY_SUMMARY_CHUNK_LIMIT` = 10 chunks (`:22`, `:1785`) until a deadline.
Intermediate summaries stay in memory; **nothing is written unless every batch
completes** (`:1752`–`:1800`). Topic keywords come back from the same call.

**5. The row, its FTS index and its embedding are written.**
`db.upsert_conversation_summary` (`db.py:317`) inserts or updates on the
`(date_utc, source_app)` unique key, preserving the existing row id. The `AFTER
INSERT/UPDATE/DELETE` triggers (`db.py:56`) maintain `summaries_fts`, which is
an external-content FTS5 table with the Porter tokenizer (`db.py:47`). The
embedding is regenerated only when `db.has_vector_store` and
`cfg.embedding_model` are both present (`conversation.py:1833`–`:1840`).

**6. The watermark advances.** Only after a real write, and only if the
snapshot is still current, `mark_saved_up_to(snapshot_timestamp)` is called
(`:2200`–`:2203`). A superseded or failed snapshot leaves the diary untouched
and the chunks pending.

**7. The graph is updated from that summary.** In the same flush, and
non-fatally, a fresh `GraphMemoryStore(db.db_path)` (`:2213`) is opened and
`update_graph_from_dialogue` (`graph_ops.py:897`) runs: the summary is passed
to `extract_graph_memories` (`:120`), whose candidates are filtered by a
semantic review that keeps only `DURABLE` facts (`_review_graph_facts`, `:83`);
an unknown branch label defaults to User (`:313`). Each surviving fact is
placed with a branch-pinned traversal, then each destination node is rewritten
by `merge_node_data` (`:617`, called at `:1028`) or, if that merge fails,
plainly appended via `append_to_node` (`graph.py:552`). A node past
`SPLIT_THRESHOLD` tokens is passed to `auto_split_node` (`graph_ops.py:767`). Graph
extraction is wrapped so a failure cannot lose the diary write
(`conversation.py:2209`).

**8. The Postgres store is written only by a human path or the importer.**
Nothing in the flush or the reply engine calls `upsert_document`. The panel's
Memory page is the writer: `save_note` and `delete_note` in
`src/claire/panel/server.py` route to `_save_note_postgres` and
`_delete_note_postgres` when the backend is Postgres, so what the browser writes
is what the router row and the chunk table hold. The one-shot importer
(`migrate_md_to_pg.py:214`) is the other writer, used to bring an existing vault
across. In `vault` mode the same panel calls fall through to
`notes.create_note` / `notes.update_note` (`notes.py:761`, `:880`), and
`router.ensure_router` (`router.py:307`) creates `CLAIRE.md` from
`router_template` (`router.py:115`) when it is absent. There is no note-writing
tool in the tool registry under `src/claire/tools/`.

### What is read, and where it enters the prompt

Order inside one `run_reply_engine` turn:

1. **Recall decision.** The planner decides `plan_demands_memory`; an empty
   plan fails open to `needs_memory = True` (`engine.py:1310`–`:1311`).
2. **Recall gate.** When the plan did not explicitly ask for memory and a hot
   window exists, `should_recall` (`recall_gate.py:50`) is consulted
   (`engine.py:1321`–`:1329`). It returns skip only if the window already holds
   a tool message (`is_tool_message`, `conversation.py:676`) *and* at least
   `min_coverage` = 0.5 of the query's content words appear in the window text
   (`recall_gate.py:54`, `:83`). It never raises outward: any error is logged
   and recall continues (`engine.py:1329`).
3. **Warm profile.** `build_warm_profile` (`graph_ops.py:1207`) walks the User
   and Directives subtrees and returns a `{"user", "directives"}` dict; the
   rendered block is cached on the `DialogueMemory` hot cache for the
   conversation and invalidated by a graph mutation listener registered in the
   daemon (`engine.py:1354`–`:1397`, `_invalidate_wp_on_graph_mutation` at
   `daemon.py:1775`, registered at `:1803`).
4. **Extractor.** When memory is needed, an LLM pass turns the query into
   `keywords` and implicit `questions`, and it gets its own short ceiling,
   `llm_extractor_timeout_sec` (`engine.py:1094`, default `8.0`), rather
   than inheriting the tool-execution one. The call is not retried after a
   timeout. Because the extractor is on the pre-chat critical path, the engine
   starts it **speculatively** alongside the tool router
   (`engine.py:1116`, `memory_prefetch.py:179`) and reuses the result only when
   the turn reaches memory recall with no planner topic hint
   (`engine.py:1452`–`:1456`); otherwise the serial call runs. A
   prefetch whose conversation was cleared cannot repopulate the cache: it
   publishes only when its captured `hot_cache_epoch` still matches
   (`memory_prefetch.py:161`, `conversation.py:1306`).
5. **Diary recall.** With `memory_enrichment_source` in `("all", "diary")`,
   `search_conversation_memory_by_keywords` (`conversation.py:1854`) builds an
   FTS `OR` query (capped at 5 keywords) and embeds the keyword cluster for
   `db.search_hybrid` (`db.py:177`): candidate pools of
   `max(top_k * 3, 50)` per source fused by weighted reciprocal rank fusion,
   `0.6 / (60 + vector_rank) + 0.4 / (60 + keyword_rank)`, equal values sharing
   a rank (`db.py:95`). With no query vector the FTS order is kept; with no
   usable keyword query the newest summaries are returned (`db.py:225`–`:246`).
   The engine caps results at `cfg.memory_enrichment_max_results`
   (`engine.py:1497`).
6. **Graph recall.** With the source in `("all", "graph")` and at least two
   content words in the implicit questions, `search_nodes` (`graph.py:684`) is
   queried with `limit=5` (`engine.py:1549`); each hit is prefixed with its
   branch/ancestor path.
7. **Digest.** For small models (auto, or forced by `memory_digest_enabled`),
   the raw diary and graph text is replaced by one relevance-filtered note from
   `digest_memory_for_query` (`engine.py:1598`;
   `src/claire/reply/enrichment.py:346`).
8. **Prompt assembly.** `build_system_prompt(name, load_default_router=True)`
   (`engine.py:1735`) loads the router and wraps it under
   `## Memory (always loaded)` (`system_prompt.py:273`). In `postgres` mode the
   read is `PostgresMemoryRouter.reload()`, which reads the `memory_documents`
   router row; the budget and line-boundary truncation are the inherited file
   router's (`router.py:195`, `:242`). The warm profile is
   appended next, unconditionally and outside the planner gate
   (`engine.py:1760`), then the diary block (`:1771`), the graph block
   (`:1784`) and the digest (`:1794`). Each block carries framing text: diary
   entries are reference-only with newest-wins, and anything the digest
   attributes to the assistant must be re-verified with a tool before restating.

### Retrieval outside the reply path

`search_conversation_memory` (`conversation.py:1963`) and
`get_relevant_conversation_context` (`:2105`) are the natural-language
wrappers over the same hybrid search, used by callers that want "top N matches"
without the engine's planner gating. The maintenance sweeps are user-triggered
from the memory viewer, not the daemon:
`rewrite_all_diary_summaries` (`:226`) and `optimise_diary_topics` (`:462`).

Notes are **not** injected into the reply prompt. The Postgres store exposes
`search()` (`pg.py:727`) — the hybrid vector + full-text call — over
`semantic_search` and `keyword_search` (`:664`, `:701`); the panel's Memory page
is its caller, so a note is found by meaning rather than by a human following a
pointer. The router remains the only part of this store that enters a prompt
automatically.

## Configuration

Defaults are read from `src/claire/config.py`; the keys below are the ones that
change memory behaviour. Every value is overridable per key in the config file.

| Key | Default | Effect |
|---|---|---|
| `db_path` | `~/.local/share/claire/claire.db` (`config.py:83`, `:643`) | the single SQLite file holding the diary *and* the graph |
| `sqlite_vss_path` | `None` (`:644`) | optional sqlite-vss extension; when absent a FAISS or Python store is used (`db.py:119`–`:135`) |
| `vault_dir` | `~/.local/share/claire/vault` (`config.py:89`, `:647`) | the **legacy** markdown vault, used only when the backend is `vault`; the retired router and `notes/` live here |
| `memory_backend` | `"postgres"` (`:650`) | `postgres` (the default) or `vault`; the environment variable `CLAIRE_MEMORY_BACKEND` outranks it, and an unrecognised value fails safe to `postgres` (`system_prompt.py:176`) |
| `pg_dsn` | `""` (`:651`) | optional DSN override; empty means read `CLAIRE_PG_DSN` from the environment or `pg.env` (`pg.py:155`) |
| `dialogue_memory_timeout` | `300.0` (`config.py:840`) | the short-term window *and* the forced-diary interval; feeds `RECENT_WINDOW_SEC` / `MAX_UNSAVED_AGE_SEC` |
| `dialogue_memory_max_tokens` | `8000` (`:843`) | ceiling on history sent to the model; `0` disables the dimension |
| `dialogue_memory_max_messages` | `40` (`:844`) | second ceiling on that history |
| `memory_enrichment_max_results` | `3` (`:845`) | diary hits passed to the prompt |
| `memory_enrichment_source` | `"all"` (`:846`) | `"all"`, `"diary"` or `"graph"`; an unknown value falls back to `"all"` (`:1283`–`:1284`) |
| `tool_carryover_max_turns` | `2` (`:848`) | prior tool turns re-injected into the hot window |
| `tool_carryover_per_entry_chars` | `1200` (`:849`) | per-entry cap on that carryover |
| `memory_digest_enabled` | `None` (`:851`) | `None` = auto (on for small models), `true`/`false` force it |
| `tool_result_digest_enabled` | `None` (`:857`) | same tri-state for distilling raw tool results |
| `llm_extractor_timeout_sec` | `8.0` (`:699`) | the memory keyword extractor's own ceiling, so a hung extractor cannot hold the pre-chat phase open for the tool-execution timeout (`engine.py:1094`) |
| `embedding_provider` | `""` (`config.py:671`) | `""` = follow `llm_provider`; or `"ollama"`, `"openai_compatible"`, `"gemini"` (`"google"` accepted as an alias) |
| `embedding_model` | provider-aware (`:1064`–`:1075`) | `ollama_embed_model` on the Ollama/OpenAI-compatible paths, `gemini-embedding-2` on the Gemini path; set it to `gemini-embedding-001` for the text-only model |
| `embedding_base_url` | `""` (`:672`) | per-provider override; the Gemini default is `https://generativelanguage.googleapis.com/v1beta` |
| `embedding_api_key` | `""` (`:673`) | empty on the Gemini path reads `GEMINI_API_KEY`, then `GOOGLE_API_KEY`; the chat key is never reused |

**The vector index is 768 floats wide, and embedding backends are pinned to
it.** The sqlite-vss table is declared `vss0(vec(768))` (`db.py:69`), the
fallback store is constructed with `dimension=EMBEDDING_DIMENSION` (`db.py:135`,
the constant lives in `utils/embedding_vector.py`), and the Postgres store
declares `embedding vector(768)` with `EMBEDDING_DIM = 768`
(`pg.py:32`). Neither can be migrated in
place, so the dimension is a storage contract rather than a config key: the
Gemini client sends `outputDimensionality: 768` and discards any vector that
comes back a different length (`llm/gemini.py`). The pure-Python fallback store
is width-agnostic and skips mismatched pairs at search time
(`utils/vector_store.py:102`). Switching embedding *models* is still a
re-index even when the width matches: Google documents
`gemini-embedding-001` and `gemini-embedding-2` as living in incompatible
embedding spaces.

The two sides of a retrieval carry different task types — `RETRIEVAL_DOCUMENT`
when a summary is indexed (`conversation.py:356`, `:616`, `:1838`) and
`RETRIEVAL_QUERY` when memory is searched (`:1908`, `:1999`) — because symmetric
usage is measurably worse for retrieval. A failed embed is logged and falls back
to keyword search; it never writes a vector of the wrong shape.

**The Postgres DSN lives outside the repository.** The two DSNs are
`~/.config/claire/pg.env` (mode 0600, containing a password): `CLAIRE_PG_DSN`
for the production database and `CLAIRE_PG_TEST_DSN` for the test database.
Never print, log or quote a DSN. The systemd user units pick the file up through
a drop-in `~/.config/systemd/user/<unit>.service.d/pg-env.conf` whose
contents are `EnvironmentFile=-%h/.config/claire/pg.env`. Resolution order in
`resolve_dsn()` (`pg.py:155`) is explicit argument, then `CLAIRE_PG_DSN` in the
environment, then `config.json`'s `pg_dsn`, then the file, then assembled
`CLAIRE_PG_*` parts. The runbook in [`docs/PGVECTOR.md`](../PGVECTOR.md) has the
installation and migration commands.

**The legacy vault path has an environment override:** `CLAIRE_VAULT_DIR`
(`notes.py:81`). Resolution order is explicit argument, then the environment
variable, then the `vault_dir` config key, then the built-in default
(`notes.py:145`–`:153`). The environment variable outranks the config file on
purpose, as the per-run override a test or a one-off command can rely on
(`notes.py:141`–`:143`). The router's byte
cap is not a config key: it is `ROUTER_BUDGET_BYTES` = 6000 (`notes.py:93`),
overridable per file by the router's own `budget_bytes` frontmatter
(`router.py:242`). The Postgres router row carries the same `budget_bytes`
field, and the byte cap applies identically.

## Extension points

- **A new memory backend.** `_memory_backend()` (`system_prompt.py:176`) is the
  single selector, and `load_router_from_vault` (`:204`) is the single seam the
  prompt path goes through. A new store must return an object exposing
  `render_for_prompt() -> str`; anything else in the prompt path is shared.
- **A new memory store or a fifth branch.** `FIXED_BRANCHES`
  (`graph.py:124`) is the one place the taxonomy is declared; `FIXED_BRANCH_IDS`
  (`:148`) and `_ensure_root` (`:268`) seed it idempotently. Adding a branch is
  a schema-shaped change, so update `graph.spec.md` first.
- **Reacting to graph writes.** `register_graph_mutation_listener`
  (`graph.py:38`) is the observer seam; callbacks get `action`, `node_id` and
  `branch`, and listener exceptions are swallowed. The only production listener
  is the warm-profile cache invalidation in `_invalidate_wp_on_graph_mutation`
  (`daemon.py:1775`), wired in `daemon.main`.
- **Serving a router into a prompt.** Anything exposing
  `render_for_prompt() -> str` can be passed to `build_system_prompt`
  (`system_prompt.py:391`); `load_default_router=True` forces the read,
  `None` reads it only when it already exists, and `False` leaves the legacy
  prompt (`_resolve_memory_section`, `:301`–`:389`). This is exactly the
  contract `PostgresMemoryRouter` satisfies by subclassing the file router.
- **A new document kind.** `upsert_document` (`pg.py:433`) and the
  `memory_documents.doc_kind` column are the extension point; the unique partial
  index keeps at most one `'router'` row.
- **Vault tooling.** `obsidian.py` owns vault shape, not storage:
  `describe_vault` (`:132`), `is_obsidian_vault` (`:151`), `ensure_vault`
  (`:157`), wiki-link helpers (`wiki_link` `:189`, `extract_wiki_links` `:209`,
  `link_targets_in_text` `:219`, `resolve_wiki_link` `:239`) and
  `validate_note_document` (`:260`). It is wired into the panel
  (`src/claire/panel/server.py`), which calls `describe_vault` for the vault
  tab, the wiki-link helpers and validator when reading a note, `ensure_vault`
  before a write and `link_targets_in_text` for backlinks.
  Nothing on the reply path imports it. `unreferenced_notes`
  (`notes.py:688`) counts `[[name]]`, `notes/name.md` and `name.md` pointers
  (`notes.py:674`, `:699`), which is what keeps the Obsidian-shaped vault and the
  router-honesty report agreeing. All of this describes the legacy path and the
  export format, not the live store.
- **Panel surfaces.** In `src/claire/panel/server.py`, `PanelState.router_status`
  (with its Postgres branch, `_router_status_postgres`), `.diary`,
  `.memory_notes` and `.memory_search`
  expose the store and diary read-only to the browser; the graph explorer and
  the maintenance sweeps live in `src/desktop_app/memory_viewer.py`
  (`get_graph_store`, and `diary_scrub_deflections` / `diary_optimise_topics`
  for the rewrite and topic sweeps).
- **Maintenance sweeps.** `consolidate_all_populated_nodes`
  (`graph_ops.py:1105`) is the graph-side entry point for dedupe/merge over
  every populated node; it is invoked from the viewer, not the daemon.

## Failure modes

Only behaviours the code actually implements are listed.

- **SQLite is busy or locked.** The writer lock is an in-process `RLock`
  (`db.py:115`); cross-process contention is left to SQLite. The daemon only
  starts a diary flush when it holds the chat-query lock (`daemon.py:1998`), so
  a user reply is never serialised behind a multi-minute summarisation; a
  skipped tick is retried on the next 60 s cycle. A failed write rolls back
  (`db.py:353`).
- **SQLite is corrupt or cannot be opened.** `Database.__init__` closes the
  connection and re-raises if `_init_schema` fails (`db.py:145`–`:149`), so the
  daemon fails at start-up rather than running with a half-built store. The
  graph store is opened separately and any boot failure there is logged as
  non-fatal (`daemon.py:1815`–`:1820`).
- **sqlite-vss is missing or fails to load.** The flag simply stays false and a
  FAISS or Python store at 768 dimensions is used instead (`db.py:119`–`:135`,
  `:152`); `has_vector_store` then decides whether an embedding is attempted at
  all. The diary still reads correctly: search takes the FTS-only or newest-rows
  code paths (`db.py:225`–`:246`).
- **Embedding backend fails or times out.** `embedder.embed_text`
  (`embedder.py:84`) returns `None` on any failure and the diary fallback
  `_embed_text` (`conversation.py:74`) does the same; a diary write still
  succeeds and retrieval falls back to keywords. An embedding refresh is
  best-effort in the sweeps too. The per-turn budget is clamped because a cold
  backend was measured in seconds while a warm one is milliseconds.
- **The memory database is unavailable or not migrated.** In `postgres` mode a
  missing or unreachable database surfaces as a visible
  `[memory-unavailable]` section rather than silent amnesia:
  `_resolve_memory_section` turns the raise from `load_router_from_postgres`
  into that marker (`system_prompt.py:301`, `:273`, `:132`). An empty router
  render becomes `[memory-empty]`, and an over-budget one is prefixed with
  `[memory-over-budget]` (`system_prompt.py:133`, `:134`).
  `pg.health` (`pg.py:298`) is the reachability probe the self-check uses.
- **The router row is absent.** `router_exists` (`pg_router.py:85`) reports it.
  An automatic (`load_default_router=None`) read returns nothing rather than
  fabricating a document, so a machine with no router row keeps a prompt without
  the memory section; a forced read raises `RouterNotFoundError` from `reload`
  (`pg_router.py:55`), which becomes `[memory-unavailable]`. The router is
  created by the importer or from the panel, never silently.
- **The router is over budget.** The body is truncated at a line boundary and a
  loud warning naming the real size, budget and dropped bytes is prepended
  (`router.py:195`, `:99`–`:107`). `size_bytes`/`over_budget`/`dropped_bytes`
  describe the document, not the last render (`:242`–`:262`). The Postgres
  router inherits this path unchanged.
- **The vault is missing (legacy path only).** When the backend is `vault`, the
  reply engine passes `load_default_router=True`, and `load_router` defaults to
  `create=True`, so the missing file is created from the template
  (`router.py:115`, `:348`); creation is reported, never silent. A vault
  directory is returned even when it does not exist yet (`notes.py:153`);
  `create_note` creates it on demand.
- **The vault is unreadable or not decodable (legacy path only).** A real read
  failure raises a named error (`RouterReadError` / `RouterDecodeError`,
  `router.py:67`, `:71`)
  and `build_memory_section` turns it into a visible
  `[memory-unavailable]` section instead of an empty prompt
  (`system_prompt.py:273`). An empty render becomes `[memory-empty]`.
- **The note index is missing (legacy path only).** `_require_notes_dir`
  (`notes.py:397`–`:408`)
  raises a `NotesDirectoryError` naming the directory rather than returning an
  empty list; it is reached from `list_notes` (`:501`), `search_notes`
  (`:585`), `unreferenced_notes` (`:688`), `update_note` (`:880`) and
  `delete_note` (`:938`). `read_note` resolves the path itself and raises
  `NoteNotFoundError` naming both the note and the directory it looked in
  (`:529`, `:535`). A note name that would
  escape the vault is refused twice: at normalisation (`:328`) and again at
  path resolution (`:378`–`:394`).
- **Graph extraction fails.** It is wrapped so the diary write still counts as
  success (`conversation.py:2209`). The semantic fact review fails closed for
  the graph cycle: an unavailable or malformed review returns no facts rather
  than unreviewed ones (`graph_ops.py:88`, `:112`).
- **The recall gate fails.** Any exception is caught and treated as "recall"
  (`engine.py:1329`), and a stopword-only or empty query cannot justify a skip
  (`recall_gate.py:71`–`:74`).

## Where the tests live

| Area | Test files |
|---|---|
| Postgres store, importer and Postgres router | `tests/test_memory_pg.py`, `tests/test_memory_pg_router.py` |
| Diary store, hybrid retrieval, embeddings | `tests/test_hybrid_memory_search.py`, `test_diary_embedding_failure.py`, `test_diary_embedding_freshness.py`, `test_diary_fallback_index.py`, `test_embedding_timeout_wiring.py`, `test_diary_summary_identity.py` |
| Summariser, flush and hygiene | `test_diary_enrichment_flow.py`, `test_diary_generation_budget.py`, `test_diary_pending_batches.py`, `test_diary_poisoning_defence.py`, `test_diary_summary_identity.py`, `test_diary_graph_logging.py`, `test_diary_import.py` |
| Maintenance sweeps | `test_diary_rewrite_sweep.py`, `test_diary_topic_optimise.py`, `test_memory_viewer_diary_scrub_api.py`, `test_memory_viewer_diary_optimise_api.py` |
| Short-term window, carryover and budget | `test_dialogue_memory.py`, `test_dialogue_memory_hot_cache.py`, `test_dialogue_memory_tool_carryover.py`, `test_context_budget.py`, `test_memory_injected_per_request.py` |
| Knowledge graph | `test_graph_memory.py`, `test_graph_ops.py`, `test_graph_memory_tools.py`, `test_graph_fact_hygiene.py`, `test_graph_timing_context.py`, `test_graph_mutation_listener.py`, `test_memory_viewer_graph_api.py` |
| Router, notes, vault shape and recall gate | `test_memory_router.py`, `test_memory_notes.py`, `test_memory_obsidian.py` (vault shape and the full resolution order), `test_panel_memory.py` (the panel's note round-trip through `notes`/`obsidian`), `test_recall_gate.py` |
| Enrichment and digest | `test_enrichment.py`, `test_enrichment_model_routing.py`, `test_memory_digest_generation_budget.py`, `test_memory_recall_generation_budget.py`, `test_memory_extractor_prefetch.py` (the speculative extractor and its epoch guard), `test_extractor_timeout_ceiling.py` (the extractor's own ceiling) |
| Evaluations | `evals/test_hybrid_retrieval.py` (ranking, not a specific embedding model), `evals/test_graph_branch_routing.py`, `evals/test_knowledge_extraction.py`, `evals/test_merge_consolidation.py`, `evals/test_memory_digest_identity.py`, `evals/test_memory_digest_preferences.py`, `evals/test_memory_digest_relevance.py` |

`test_memory_notes.py` monkeypatches `CLAIRE_VAULT_DIR` away from the real data
directory, and `test_memory_router.py` works entirely in `tmp_path`, so neither
reads the owner's vault. The pre-existing suite pins `vault` explicitly in
`tests/conftest.py`; tests that exercise Postgres opt in with
`monkeypatch.setenv("CLAIRE_MEMORY_BACKEND", "postgres")` and skip themselves
when the test database is unreachable.

## Documentation drift

These are places where a spec or a `docs/*.md` file disagrees with the code as
it stands. Both sides are given.

1. **Auto-merge is documented as future work but is live.**
   [`graph.spec.md:133`](../../src/claire/memory/graph.spec.md#L133) is headed
   "Auto-Merge (Future — requires LLM integration)". `merge_node_data`
   (`src/claire/memory/graph_ops.py:617`) is implemented and is called
   automatically inside `update_graph_from_dialogue`
   (`graph_ops.py:1028`), with the plain-append fallback at `:1063`–`:1074`.
   The same spec's "Housekeeping (Future)" (`:142`) remains accurate: no
   periodic promoter/compressor runs; `consolidate_all_populated_nodes`
   (`graph_ops.py:1105`) is operator-triggered only.

2. **"Built in parallel" contradicts the spec's own default and the config.**
   [`graph.spec.md:248`](../../src/claire/memory/graph.spec.md#L248) says "the
   diary system remains the primary memory system for now; the graph is a v2
   system being built in parallel". The same file records the default flipping
   to include graph recall (`graph.spec.md:190`), and the shipped default is
   `"memory_enrichment_source": "all"` (`src/claire/config.py:846`), with the
   warm profile injected on every turn (`src/claire/reply/engine.py:1760`).

3. **The vault-shape comments point at `docs/OBSIDIAN.md`.** `src/claire/config.py:122`,
   `src/claire/memory/notes.py:30` and `src/claire/memory/obsidian.py:46` all
   cite that page as the reader-facing companion to `obsidian.py`. That page is
   now scoped to the retired vault and the one-way export, so the comments
   describe the legacy path only; the live store is documented in
   [`docs/PGVECTOR.md`](../PGVECTOR.md) and [`docs/MEMORY.md`](../MEMORY.md).

## Not covered here

The SQLite-backed *meals* table lives in the same file and module
(`db.py:19`) but is a nutrition log, not memory; `docs/` does not currently
have a spoke for it. Voice capture, the intent judge and the reply loop are
covered by the [listening](listening.md) and
[intent-and-reply](intent-and-reply.md) spokes.
