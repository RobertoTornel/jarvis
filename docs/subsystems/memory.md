# Long-term memory

**Authoritative specs:** [`db.spec.md`](../../src/claire/memory/db.spec.md)
(the diary store, FTS5 and hybrid retrieval),
[`summariser.spec.md`](../../src/claire/memory/summariser.spec.md)
(diary generation and the deflection/topic sweeps),
[`graph.spec.md`](../../src/claire/memory/graph.spec.md) (the knowledge graph),
[`router.spec.md`](../../src/claire/memory/router.spec.md) (the always-loaded
`CLAIRE.md`) and
[`recall_gate.spec.md`](../../src/claire/memory/recall_gate.spec.md)
(the skip heuristic). Read this when you are changing what Claire remembers,
where it is stored, what is recalled into a reply, or the markdown vault.

## Responsibility

Long-term memory is four stores with four different lifetimes. None of them is a
conversation log: the live conversation lives in `DialogueMemory` in the daemon
process only, and long-term memory is what survives it.

| Store | Medium | Written by | Recalled into a reply |
|---|---|---|---|
| **Diary** — one row per `(date_utc, source_app)` | `conversation_summaries` in SQLite, with FTS5 and optional vector index | the background diary flush, after the short-term window closes | keyword/vector search for the current query |
| **Knowledge graph** — self-organising node tree | `memory_nodes` in the same SQLite file | fact extraction after a successful diary write | the always-on *warm profile* plus query-driven node search |
| **Router** — `<vault>/CLAIRE.md` | one markdown file outside the repository | a human, via the panel's memory editor | re-read from disk on **every** request, into the system prompt |
| **Notes** — one markdown file per fact | `<vault>/notes/*.md` | a human, via the panel's memory editor | only when a pointer is followed by hand; never auto-loaded |

Three properties follow from that split. The diary and the graph are the only
stores the model writes indirectly (through summarisation and extraction, never
through a tool call). The vault is never written by the model at all. And the
router plus the graph's warm profile are re-assembled into the prompt on every
turn, so correcting a file corrects behaviour without a restart
(`router.py:278`, `system_prompt.py:233`, `engine.py:1529`).

## Key modules

| Module | Owns | Anchors |
|---|---|---|
| `src/claire/memory/db.py` | SQLite schema, FTS5 triggers, optional sqlite-vss/FAISS/Python vector index, weighted reciprocal rank fusion | `_SCHEMA_SQL` `:10`, `conversation_summaries` `:37`, `summaries_fts` `:47`, FTS triggers `:56`, `_VSS_SCHEMA_SQL` `:68`, `_reciprocal_rank_fusion` `:95`, `Database.__init__` `:110`, `has_vector_store` `:147`, `_init_schema` `:151`, FTS rebuild marker `:159`, `search_hybrid` `:172`, `upsert_conversation_summary` `:312`, `upsert_summary_embedding` `:404`, `close` `:442` |
| `src/claire/memory/conversation.py` | short-term `DialogueMemory`, the diary summariser, history budgeting, tool carryover, keyword/vector search, the deflection and topic sweeps | `_DIARY_SUMMARY_CHUNK_LIMIT` `:17`, `_embed_text` `:69`, `_rewrite_diary_summary` `:123`, `rewrite_all_diary_summaries` `:200`, `optimise_diary_topics` `:435`, `is_tool_message` `:648`, `bound_conversation_history` `:829`, `DialogueMemory` `:935`, `RECENT_WINDOW_SEC` `:1010`, `add_message` `:1039`, `record_tool_turn` `:1152`, `get_recent_turns_with_tools` `:1287`, `get_pending_chunks_with_snapshot` `:1387`, `should_update_diary` `:1421`, `mark_saved_up_to` `:1454`, `generate_conversation_summary` `:1494`, `update_daily_conversation_summary` `:1659`, `search_conversation_memory_by_keywords` `:1799`, `search_conversation_memory` `:1905`, `update_diary_from_dialogue_memory` `:2067` |
| `src/claire/memory/graph.py` | node schema, fixed branches, traversal, decay, mutation listeners | `register_graph_mutation_listener` `:38`, `SPLIT_THRESHOLD` `:89`, `FIXED_BRANCHES` `:124`, `FIXED_BRANCH_IDS` `:148`, `_GRAPH_SCHEMA_SQL` `:216`, `GraphMemoryStore` `:241`, `_ensure_root` `:268`, `migrate_legacy_shape` `:308`, `append_to_node` `:552`, `touch_node` `:568`, `get_recent_nodes` `:582`, `get_top_nodes` `:594`, `get_ancestors` `:642`, `search_nodes` `:684`, `get_graph_data` `:754` |
| `src/claire/memory/graph_ops.py` | extraction, semantic review, placement, merge, split, warm profile | `_review_graph_facts` `:83`, `extract_graph_memories` `:120`, unknown-branch fallback `:313`, `merge_node_data` `:617`, `auto_split_node` `:767`, `update_graph_from_dialogue` `:897`, automatic merge `:1028`, `consolidate_all_populated_nodes` `:1105`, `build_warm_profile` `:1207`, `format_warm_profile_block` `:1235` |
| `src/claire/memory/notes.py` | vault path resolution, note frontmatter, note read/write/search, dangling-pointer report — no PyYAML | `VAULT_ENV_VAR` `:82`, `ROUTER_FILENAME` `:91`, `ROUTER_BUDGET_BYTES` `:94`, `resolve_vault_dir` `:133`, config-key step `:151`, `resolve_notes_dir` `:217`, `resolve_router_path` `:230`, `parse_frontmatter` `:249`, `normalize_note_name` `:329`, `list_notes` `:502`, `read_note` `:516`, `search_notes` `:586`, `unreferenced_notes` `:689`, `create_note` `:762`, `ensure_notes_dir` `:839`, `update_note` `:881`, `delete_note` `:939` |
| `src/claire/memory/router.py` | reading, byte-accounting, truncating and rendering `CLAIRE.md`; template creation | `MEMORY_HEADER` `:82`, `router_template` `:115`, `_truncate_body` `:195`, `MemoryRouter` `:221`, `budget_bytes` `:238`, `reload` `:262`, `render_for_prompt` `:278`, `ensure_router` `:303`, `load_router` `:344` |
| `src/claire/memory/recall_gate.py` | deterministic pre-flight check that can skip enrichment | `should_recall` `:48`, `min_coverage` `:52`, coverage computation `:82` |
| `src/claire/memory/obsidian.py` | Obsidian-compatible vault *shape* — `.obsidian/`, wiki links, document validation, conventions — not a store. Library-only from the reply path's point of view; wired into the panel's vault browser | `VaultLayout` `:99`, `OBSIDIAN_DIRNAME` `:82`, `describe_vault` `:132`, `is_obsidian_vault` `:151`, `ensure_vault` `:157`, `wiki_link` `:189`, `extract_wiki_links` `:209`, `link_targets_in_text` `:219`, `resolve_wiki_link` `:239`, `validate_note_document` `:260`, `vault_conventions` `:331` |

`src/claire/memory/__init__.py` is empty: the package deliberately re-exports
nothing, so every caller imports the submodule it needs (for example
`from claire.memory import notes as notes_module`). There is no aggregate
`memory` API to extend.

Two modules outside `memory/` finish the picture: `src/claire/system_prompt.py`
renders the vault into the prompt (`MEMORY_SECTION_HEADING` `:130`,
`resolve_default_router_path` `:165`, `load_router_from_vault` `:172`,
`build_memory_section` `:233`, `build_system_prompt` `:330`), and
`src/claire/reply/engine.py` decides what is recalled
(`:1104`–`:1125` gating, `:1148` warm profile, `:1194` enrichment source,
`:1238` extractor, `:1259` diary, `:1297` graph, `:1367` digest, `:1506`–`:1512`
router, `:1529`–`:1583` prompt injection).

## Data and control flow

### What is written, in what order

**1. The short-term window fills.** `DialogueMemory.add_message`
(`conversation.py:1039`) appends `(timestamp, role, content)` under a lock and
assigns a strictly monotonic timestamp. `RECENT_WINDOW_SEC` and
`MAX_UNSAVED_AGE_SEC` are both the configured `dialogue_memory_timeout`
(`:1010`–`:1011`); `bound_conversation_history` (`:829`) caps what is *sent* to
the model (`dialogue_memory_max_tokens` / `_max_messages`) without capping what
is stored. Tool messages from the current conversation are re-recorded by
`record_tool_turn` (`:1152`) and re-injected by `get_recent_turns_with_tools`
(`:1287`).

**2. The flush trigger fires.** `should_update_diary` (`:1421`) returns true
when there are unsaved messages and either the user has been inactive for the
window or the oldest unsaved message has reached `MAX_UNSAVED_AGE_SEC`. The
daemon polls this every 60 s while it can take the chat-query lock
(`daemon.py:1628`, `:1696`–`:1702`) and forces a final flush at shutdown
(`daemon.py:1753`). `_check_and_update_diary` (`daemon.py:797`) resolves
`source_app` to `"stdin"` or `"voice"` (`:861`).

**3. Pending work is snapshotted.** `update_diary_from_dialogue_memory`
(`conversation.py:2067`) takes `_diary_flush_lock` non-blocking, so flushes on
one `DialogueMemory` never overlap (`:2098`), then captures the pending chunks
*and* the snapshot timestamp atomically
(`_get_pending_diary_work` / `get_pending_chunks_with_snapshot`, `:2112`,
`:1387`). Messages arriving during summarisation stay unsaved because the
watermark is the captured snapshot, not wall-clock now.

**4. The summary is generated in bounded batches.**
`update_daily_conversation_summary` (`:1659`) redacts every chunk first
(`:1687`), reads today's existing row as the previous summary (`:1696`), and
runs `generate_conversation_summary` (`:1494`) over batches of at most
`_DIARY_SUMMARY_CHUNK_LIMIT` = 10 chunks (`:17`, `:1725`) until a deadline.
Intermediate summaries stay in memory; **nothing is written unless every batch
completes** (`:1723`–`:1776`). Topic keywords come back from the same call.

**5. The row, its FTS index and its embedding are written.**
`db.upsert_conversation_summary` (`db.py:312`) inserts or updates on the
`(date_utc, source_app)` unique key, preserving the existing row id. The `AFTER
INSERT/UPDATE/DELETE` triggers (`db.py:56`) maintain `summaries_fts`, which is
an external-content FTS5 table with the Porter tokenizer (`db.py:47`). The
embedding is regenerated only when `db.has_vector_store` and
`cfg.embedding_model` are both present (`conversation.py:1781`–`:1786`).

**6. The watermark advances.** Only after a real write, and only if the
snapshot is still current, `mark_saved_up_to(snapshot_timestamp)` is called
(`:2139`–`:2142`). A superseded or failed snapshot leaves the diary untouched
and the chunks pending.

**7. The graph is updated from that summary.** In the same flush, and
non-fatally, a fresh `GraphMemoryStore(db.db_path)` (`:2152`) is opened and
`update_graph_from_dialogue` (`graph_ops.py:897`) runs: the summary is passed
to `extract_graph_memories` (`:120`), whose candidates are filtered by a
semantic review that keeps only `DURABLE` facts (`_review_graph_facts`, `:83`);
an unknown branch label defaults to User (`:313`). Each surviving fact is
placed with a branch-pinned traversal, then each destination node is rewritten
by `merge_node_data` (`:617`, called at `:1028`) or, if that merge fails,
plainly appended via `append_to_node` (`graph.py:552`). A node past
`SPLIT_THRESHOLD` tokens is passed to `auto_split_node` (`:767`). Graph
extraction is wrapped so a failure cannot lose the diary write
(`conversation.py:2148`).

**8. The vault is written only by a human path.** Nothing in the flush or the
reply engine calls `create_note` / `update_note`. The panel's memory editor is
the writer (`panel/server.py:2199`, `:2206`, `:2223`), and
`router.ensure_router` (`router.py:303`) creates `CLAIRE.md` from
`router_template` (`:115`) when it is absent. There is no note-writing tool in
the tool registry under `src/claire/tools/`.

### What is read, and where it enters the prompt

Order inside one `run_reply_engine` turn:

1. **Recall decision.** The planner decides `plan_demands_memory`; an empty
   plan fails open to `needs_memory = True` (`engine.py:1104`–`:1105`).
2. **Recall gate.** When the plan did not explicitly ask for memory and a hot
   window exists, `should_recall` (`recall_gate.py:48`) is consulted
   (`engine.py:1115`–`:1125`). It returns skip only if the window already holds
   a tool message (`is_tool_message`, `conversation.py:648`) *and* at least
   `min_coverage` = 0.5 of the query's content words appear in the window text
   (`recall_gate.py:52`, `:82`). It never raises outward: any error is logged
   and recall continues.
3. **Warm profile.** `build_warm_profile` (`graph_ops.py:1207`) walks the User
   and Directives subtrees and returns a `{"user", "directives"}` dict; the
   rendered block is cached on the `DialogueMemory` hot cache for the
   conversation and invalidated by a graph mutation listener registered in the
   daemon (`engine.py:1148`–`:1190`, `daemon.py:1486`–`:1515`).
4. **Extractor.** When memory is needed, an LLM pass turns the query into
   `keywords` and implicit `questions` (`engine.py:1238`), cached per query in
   the hot cache.
5. **Diary recall.** With `memory_enrichment_source` in `("all", "diary")`,
   `search_conversation_memory_by_keywords` (`conversation.py:1799`) builds an
   FTS `OR` query (capped at 5 keywords) and embeds the keyword cluster for
   `db.search_hybrid` (`db.py:172`): candidate pools of
   `max(top_k * 3, 50)` per source fused by weighted reciprocal rank fusion,
   `0.6 / (60 + vector_rank) + 0.4 / (60 + keyword_rank)`, equal values sharing
   a rank (`db.py:95`). With no query vector the FTS order is kept; with no
   usable keyword query the newest summaries are returned (`db.py:220`–`:238`).
   The engine caps results at `cfg.memory_enrichment_max_results`
   (`engine.py:1274`).
6. **Graph recall.** With the source in `("all", "graph")` and at least two
   content words in the implicit questions, `search_nodes` (`graph.py:684`) is
   queried with `limit=5`; each hit is prefixed with its branch/ancestor path
   (`engine.py:1297`–`:1345`).
7. **Digest.** For small models (auto, or forced by `memory_digest_enabled`),
   the raw diary and graph text is replaced by one relevance-filtered note from
   `digest_memory_for_query` (`engine.py:1367`–`:1399`;
   `src/claire/reply/enrichment.py:326`).
8. **Prompt assembly.** `build_system_prompt(name, load_default_router=True)`
   (`engine.py:1512`) re-reads `<vault>/CLAIRE.md` and wraps it under
   `## Memory (always loaded)` (`system_prompt.py:233`). The warm profile is
   appended next, unconditionally and outside the planner gate
   (`engine.py:1529`), then the diary block (`:1539`), the graph block
   (`:1560`) and the digest (`:1563`). Each block carries framing text: diary
   entries are reference-only with newest-wins, and anything the digest
   attributes to the assistant must be re-verified with a tool before restating
   (`:1540`–`:1583`).

### Retrieval outside the reply path

`search_conversation_memory` (`conversation.py:1905`) and
`get_relevant_conversation_context` (`:2044`) are the natural-language
wrappers over the same hybrid search, used by callers that want "top N matches"
without the engine's planner gating. The maintenance sweeps are user-triggered
from the memory viewer, not the daemon:
`rewrite_all_diary_summaries` (`:200`) and `optimise_diary_topics` (`:435`).

## Configuration

Defaults are read from `src/claire/config.py`; the keys below are the ones that
change memory behaviour. Every value is overridable per key in the config file.

| Key | Default | Effect |
|---|---|---|
| `db_path` | `~/.local/share/claire/claire.db` (`config.py:67`, `:547`) | the single SQLite file holding the diary *and* the graph |
| `sqlite_vss_path` | `None` (`:548`) | optional sqlite-vss extension; when absent a FAISS or Python store is used (`db.py:119`–`:138`) |
| `vault_dir` | `~/.local/share/claire/vault` (`config.py:73`, `:551`) | the markdown vault; the router and `notes/` live here |
| `dialogue_memory_timeout` | `300.0` (`config.py:728`) | the short-term window *and* the forced-diary interval; feeds `RECENT_WINDOW_SEC` / `MAX_UNSAVED_AGE_SEC` |
| `dialogue_memory_max_tokens` | `8000` (`:731`) | ceiling on history sent to the model; `0` disables the dimension |
| `dialogue_memory_max_messages` | `40` (`:732`) | second ceiling on that history |
| `memory_enrichment_max_results` | `3` (`:733`) | diary hits passed to the prompt |
| `memory_enrichment_source` | `"all"` (`:734`) | `"all"`, `"diary"` or `"graph"`; an unknown value falls back to `"all"` (`:1046`) |
| `tool_carryover_max_turns` | `2` (`:736`) | prior tool turns re-injected into the hot window |
| `tool_carryover_per_entry_chars` | `1200` (`:737`) | per-entry cap on that carryover |
| `memory_digest_enabled` | `None` (`:739`) | `None` = auto (on for small models), `true`/`false` force it |
| `tool_result_digest_enabled` | `None` (`:745`) | same tri-state for distilling raw tool results |

**The vault path has an environment override:** `CLAIRE_VAULT_DIR`
(`notes.py:82`). Resolution order is explicit argument, then the environment
variable, then the `vault_dir` config key, then the built-in default
(`notes.py:146`–`:154`). The environment variable outranks the config file on
purpose, as the per-run override a test or a one-off command can rely on
(`notes.py:142`). The router's byte
cap is not a config key: it is `ROUTER_BUDGET_BYTES` = 6000 (`notes.py:94`),
overridable per file by the router's own `budget_bytes` frontmatter
(`router.py:238`).

## Extension points

- **A new memory store or a fifth branch.** `FIXED_BRANCHES`
  (`graph.py:124`) is the one place the taxonomy is declared; `FIXED_BRANCH_IDS`
  (`:148`) and `_ensure_root` (`:268`) seed it idempotently. Adding a branch is
  a schema-shaped change, so update `graph.spec.md` first.
- **Reacting to graph writes.** `register_graph_mutation_listener`
  (`graph.py:38`) is the observer seam; callbacks get `action`, `node_id` and
  `branch`, and listener exceptions are swallowed. The only production listener
  is the warm-profile cache invalidation in `daemon.py:1486`–`:1515`.
- **Serving a router into a prompt.** Anything exposing
  `render_for_prompt() -> str` can be passed to `build_system_prompt`
  (`system_prompt.py:330`); `load_default_router=True` forces the vault read,
  `None` reads it only when it already exists, and `False` leaves the legacy
  prompt (`:261`–`:327`).
- **Vault tooling.** `obsidian.py` owns vault shape, not storage:
  `describe_vault` (`:132`), `is_obsidian_vault` (`:151`), `ensure_vault`
  (`:157`), wiki-link helpers (`wiki_link` `:189`, `extract_wiki_links` `:209`,
  `link_targets_in_text` `:219`, `resolve_wiki_link` `:239`) and
  `validate_note_document` (`:260`). It is wired into the panel, which calls
  `describe_vault` for the vault tab (`panel/server.py:1953`), the wiki-link
  helpers and validator when reading a note (`:2056`–`:2059`), `ensure_vault`
  before a write (`:2198`) and `link_targets_in_text` for backlinks (`:3486`).
  Nothing on the reply path imports it. `unreferenced_notes`
  (`notes.py:689`) counts `[[name]]`, `notes/name.md` and `name.md` pointers
  (`:675`, `:700`), which is what keeps the Obsidian-shaped vault and the
  router-honesty report agreeing.
- **Panel surfaces.** `PanelState.router_status` (`panel/server.py:1714`),
  `.diary` (`:1875`), `.memory_notes` (`:1942`) and `.memory_search` (`:2081`)
  expose the vault and diary read-only to the browser; the graph explorer and
  the maintenance sweeps live in `src/desktop_app/memory_viewer.py`
  (`get_graph_store` `:311`, rewrite sweep `:678`, topic sweep `:768`).
- **Maintenance sweeps.** `consolidate_all_populated_nodes`
  (`graph_ops.py:1105`) is the graph-side entry point for dedupe/merge over
  every populated node; it is invoked from the viewer, not the daemon.

## Failure modes

Only behaviours the code actually implements are listed.

- **SQLite is busy or locked.** The writer lock is an in-process `RLock`
  (`db.py:115`); cross-process contention is left to SQLite. The daemon only
  starts a diary flush when it holds the chat-query lock (`daemon.py:1697`), so
  a user reply is never serialised behind a multi-minute summarisation; a
  skipped tick is retried on the next 60 s cycle. A failed write rolls back
  (`db.py:348`).
- **SQLite is corrupt or cannot be opened.** `Database.__init__` closes the
  connection and re-raises if `_init_schema` fails (`db.py:140`–`:144`), so the
  daemon fails at start-up rather than running with a half-built store. The
  graph store is opened separately and any boot failure there is logged as
  non-fatal (`daemon.py:1525`–`:1533`).
- **sqlite-vss is missing or fails to load.** The flag simply stays false and a
  FAISS or Python store at 768 dimensions is used instead (`db.py:119`–`:138`,
  `:147`); `has_vector_store` reports which. If no store exists at all, the
  code takes the FTS-only or newest-rows paths (`db.py:220`–`:238`).
- **Embedding backend fails or times out.** `_embed_text`
  (`conversation.py:69`) returns `None` on any failure; the diary write still
  succeeds and retrieval falls back to keywords. An embedding refresh is
  best-effort in the sweeps too.
- **The graph tables are absent.** They are not assumed: `GraphMemoryStore`
  creates `memory_nodes` on first open (`graph.py:216`, `:262`), then
  `_ensure_root` seeds root and the three branches (`:268`). Absence is a
  first-run, not an error.
- **A legacy graph shape.** `migrate_legacy_shape` (`graph.py:308`) wipes and
  re-seeds the whole `memory_nodes` table when root has a non-fixed child or
  root's own `data` is non-empty. It runs only from daemon start-up
  (`daemon.py:1528`); the viewer and reply engine open the same store without
  triggering it. The diary is untouched, so the graph is rebuilt by
  "Import from Diary" in the memory viewer.
- **The vault is missing.** The reply engine passes
  `load_default_router=True`, and `load_router` defaults to `create=True`, so
  the missing file is created from the template (`router.py:115`, `:344`);
  creation is reported, never silent. A vault directory is returned even when
  it does not exist yet (`notes.py:139`); `create_note` creates it on demand.
- **The vault is unreadable or not decodable.** A real read failure raises a
  named error (`RouterReadError` / `RouterDecodeError`, `router.py:67`–`:71`)
  and `build_memory_section` turns it into a visible
  `[memory-unavailable]` section instead of an empty prompt
  (`system_prompt.py:223`–`:247`). An empty render becomes `[memory-empty]`.
- **The note index is missing.** `_require_notes_dir` (`notes.py:398`–`:407`)
  raises a `NotesDirectoryError` naming the directory rather than returning an
  empty list; it is reached from `list_notes` (`:512`), `search_notes`
  (`:614`), `unreferenced_notes` (`:710`), `update_note` (`:906`) and
  `delete_note` (`:951`). `read_note` resolves the path itself and raises
  `NoteNotFoundError` naming both the note and the directory it looked in
  (`:530`, `:536`). A note name that would
  escape the vault is refused twice: at normalisation (`:329`) and again at
  path resolution (`:379`–`:395`).
- **The router is over budget.** The body is truncated at a line boundary and a
  loud warning naming the real size, budget and dropped bytes is prepended
  (`router.py:195`, `:99`–`:107`). `size_bytes`/`over_budget`/`dropped_bytes`
  describe the file, not the last render (`:238`–`:258`).
- **Graph extraction fails.** It is wrapped so the diary write still counts as
  success (`conversation.py:2148`). The semantic fact review fails closed for
  the graph cycle: an unavailable or malformed review returns no facts rather
  than unreviewed ones (`graph_ops.py:88`, `:112`).
- **The recall gate fails.** Any exception is caught and treated as "recall"
  (`engine.py:1125`), and a stopword-only or empty query cannot justify a skip
  (`recall_gate.py:70`–`:72`).

## Where the tests live

| Area | Test files |
|---|---|
| Diary store, hybrid retrieval, embeddings | `tests/test_hybrid_memory_search.py`, `test_diary_embedding_failure.py`, `test_diary_embedding_freshness.py`, `test_diary_fallback_index.py`, `test_embedding_timeout_wiring.py`, `test_diary_summary_identity.py` |
| Summariser, flush and hygiene | `test_diary_enrichment_flow.py`, `test_diary_generation_budget.py`, `test_diary_pending_batches.py`, `test_diary_poisoning_defence.py`, `test_diary_summary_identity.py`, `test_diary_graph_logging.py`, `test_diary_import.py` |
| Maintenance sweeps | `test_diary_rewrite_sweep.py`, `test_diary_topic_optimise.py`, `test_memory_viewer_diary_scrub_api.py`, `test_memory_viewer_diary_optimise_api.py` |
| Short-term window, carryover and budget | `test_dialogue_memory.py`, `test_dialogue_memory_hot_cache.py`, `test_dialogue_memory_tool_carryover.py`, `test_context_budget.py`, `test_memory_injected_per_request.py` |
| Knowledge graph | `test_graph_memory.py`, `test_graph_ops.py`, `test_graph_memory_tools.py`, `test_graph_fact_hygiene.py`, `test_graph_timing_context.py`, `test_graph_mutation_listener.py`, `test_memory_viewer_graph_api.py` |
| Router, notes, vault shape and recall gate | `test_memory_router.py`, `test_memory_notes.py`, `test_memory_obsidian.py` (vault shape and the full resolution order), `test_panel_memory.py` (the panel's note round-trip through `notes`/`obsidian`), `test_recall_gate.py` |
| Enrichment and digest | `test_enrichment.py`, `test_enrichment_model_routing.py`, `test_memory_digest_generation_budget.py`, `test_memory_recall_generation_budget.py` |
| Evaluations | `evals/test_hybrid_retrieval.py` (ranking, not a specific embedding model), `evals/test_graph_branch_routing.py`, `evals/test_knowledge_extraction.py`, `evals/test_merge_consolidation.py`, `evals/test_memory_digest_identity.py`, `evals/test_memory_digest_preferences.py`, `evals/test_memory_digest_relevance.py` |

`test_memory_notes.py` monkeypatches `CLAIRE_VAULT_DIR` away from the real data
directory, and `test_memory_router.py` works entirely in `tmp_path`, so neither
reads the owner's vault.

## Documentation drift

These are places where a spec or a `docs/*.md` file disagrees with the code as
it stands. Both sides are given.

1. **Vault resolution has a config step the owner's guide does not mention.**
   [`docs/MEMORY.md:44`](../../docs/MEMORY.md#L44) says "An explicit directory
   passed to the functions always wins over the environment variable, which
   wins over the default", and `:38` presents the environment variable as *the*
   way to move the vault. The code has four steps, with the `vault_dir` config
   key between the environment variable and the default
   (`src/claire/memory/notes.py:146`–`:154`, `_configured_vault_dir` `:199`;
   declared at `src/claire/config.py:95` and loaded at `:863`–`:864`).
   `examples/config.json:4` already ships the key.

2. **The topic sweep's spec signature is the old Ollama-shaped one.**
   [`summariser.spec.md:121`](../../src/claire/memory/summariser.spec.md#L121)
   documents `optimise_diary_topics(db, ollama_base_url, ollama_chat_model, ...)`
   and `:124` says it makes "One `call_llm_direct` call to `ollama_chat_model`".
   The code is `optimise_diary_topics(db, cfg, *, embed_timeout_sec=15.0)`
   (`src/claire/memory/conversation.py:435`–`:440`) and dispatches through `cfg`
   with provider-aware helpers; `call_llm_direct` is defined in
   `src/claire/memory/graph_ops.py:37`, not in `conversation.py`.

3. **Auto-merge is documented as future work but is live.**
   [`graph.spec.md:133`](../../src/claire/memory/graph.spec.md#L133) is headed
   "Auto-Merge (Future — requires LLM integration)". `merge_node_data`
   (`src/claire/memory/graph_ops.py:617`) is implemented and is called
   automatically inside `update_graph_from_dialogue`
   (`graph_ops.py:1028`), with the plain-append fallback at `:1063`–`:1074`.
   The same spec's "Housekeeping (Future)" (`:142`) remains accurate: no
   periodic promoter/compressor runs; `consolidate_all_populated_nodes`
   (`graph_ops.py:1105`) is operator-triggered only.

4. **"Built in parallel" contradicts the spec's own default and the config.**
   [`graph.spec.md:248`](../../src/claire/memory/graph.spec.md#L248) says "the
   diary system remains the primary memory system for now; the graph is a v2
   system being built in parallel". The same file records the default flipping
   to include graph recall (`graph.spec.md:190`), and the shipped default is
   `"memory_enrichment_source": "all"` (`src/claire/config.py:734`), with the
   warm profile injected on every turn (`src/claire/reply/engine.py:1529`).

5. **The vault-shape comments point at `docs/OBSIDIAN.md`.** `src/claire/config.py:94`,
   `src/claire/memory/notes.py:30` and `src/claire/memory/obsidian.py:46` all
   cite that page, which is the reader-facing companion to `obsidian.py`; it is
   present in `docs/` in this working tree. If it is ever moved, those three
   comments move with it.

Two further inconsistencies are internal to the code rather than
spec-versus-docs, and are recorded here because they are checkable:

6. `src/claire/memory/recall_gate.py:8` describes the heuristic as "keyword
   Jaccard", but the implemented measure is asymmetric coverage,
   `|overlap| / |query_words|` (`recall_gate.py:82`), which is what
   `recall_gate.spec.md:28` describes. The module docstring is the odd one out.

7. `src/claire/memory/notes.py:51` exports `DEFAULT_VAULT_DIR` in `__all__`,
   but no such name is defined anywhere in the module; a star-import raises
   `AttributeError`. Its sibling `ROUTER_BUDGET_BYTES` (`:94`) is defined and
   fine.

## Not covered here

The SQLite-backed *meals* table lives in the same file and module
(`db.py:19`) but is a nutrition log, not memory; `docs/` does not currently
have a spoke for it. Voice capture, the intent judge and the reply loop are
covered by the [listening](listening.md) and
[intent-and-reply](intent-and-reply.md) spokes.
