# The panel's conversation view, and where conversation data actually lives

This note covers the local web panel's transcript and chat box
(`src/claire/panel/`, `R-WEB-1`). The panel faces one fact: **there is no retrievable verbatim
transcript in this codebase**, so the page shows what genuinely exists and labels it. This note
records where conversation data lives and what the panel therefore shows.

## What each place holds

| Candidate | Verdict |
| --- | --- |
| `claire.memory.conversation.DialogueMemory` | **Holds the conversation, but only in the daemon process.** `_messages` is a plain `List[Tuple[float, str, str]]` in memory. `get_recent_messages()` / `all_messages()` are callable only from inside that process. |
| A daemon endpoint to read it | **Does not exist.** `grep` for `HTTPServer`, `AF_UNIX`, `socket.socket` and friends outside `panel/` and `phone/` found nothing that serves dialogue. |
| A verbatim transcript on disk | **Does not exist.** Nothing writes an utterance to a file. |
| `conversation_summaries` (SQLite) | **Exists, and is the only persisted record** — per-day *summaries* with `date_utc`, `ts_utc`, `summary`, `topics`, `source_app`. Not turns. On this machine it holds a single row. |
| The action journal (`CLAIRE_ACTION_LOG`, `~/.config/claire/actions.jsonl`) | **Not a conversation feed, and nothing writes it.** It is shaped `at/action/outcome/detail` — what the assistant *did*, not what was *said*. `grep` shows only the panel reading it; the default path is absent on this machine. |

So the panel shows what genuinely exists, labelled with what it is, rather than pretending a summary
is a transcript, inventing a parallel store, or reading the daemon's memory, which is impossible.

## What the panel shows, and how it is labelled

**This page's own turns.** A bounded `deque` (`TranscriptBuffer`, `TRANSCRIPT_LIMIT = 50`,
lock-guarded because the server is threaded) fed by the chat form. The section is badged
`in memory` and carries `durable=False`; the note says the turns are lost when the panel
restarts, and that Claire's live voice conversation lives in memory inside the daemon, which the
panel cannot read. This is a real transcript of a real conversation — just a *small* one, and the
page says which one.

**The persisted diary.** The `conversation_summaries` rows for the last `DIARY_LIMIT = 7` days,
under a heading that says plainly *"summaries, not a verbatim transcript"*. Read-only, through
`file:<path>?mode=ro`, so the panel can never create or modify the owner's database. An absent
file is reported as absent, with its path, and never created; a missing table and a corrupt file
are each a *named* problem rather than an empty list.

## The path the chat box uses

One stack, and it is the daemon's own:

```
claire.config.load_settings()
  -> claire.llm.get_llm_backend(settings)
  -> backend.direct(model, system_prompt, user_message, timeout_sec=..., thinking=False)
```

There is **no second HTTP client** — no `requests`, no `urllib.request` in the panel module, and
a test scans the source to keep it that way. One authoritative path matters here: a panel that
quietly called the endpoint itself would drift from the daemon, exactly as a second transcription
path would drift from the one authoritative recogniser.

`direct()` reports every failure identically — it returns `None` and never raises — so the panel
genuinely cannot distinguish "endpoint down" from "model unloaded" from "credential refused".
Rather than guess, `PanelLLMError` (`502 llm-unavailable`) names the provider, the model and the
base URL, and says that the backend signals every failure the same way. An empty box is never
rendered as an answer.

## Three latent bugs, each pinned by a test

Each has a test that fails without its guard.

1. **A refusal names the route that was used.** `check_write_origin()` takes the route as an
   argument, so a refused `POST /chat` cannot blame `/select`.
2. **A body that will not be read does not stay on the connection.** The handler reads a body only
   when `Content-Length` parses; a `Transfer-Encoding: chunked` request has none, so the handler
   answers `400` and closes the connection rather than parsing the body bytes as the *next*
   request on the same connection. A test that opens a fresh connection per request cannot see
   this; the socket tests cover the keep-alive case.
3. **A relative `db_path` does not raise out of the diary read.** `Path.as_uri()` rejects relative
   paths, and a config with `"db_path": "claire.db"` is legal (`claire.config` expands `~` and
   leaves the rest alone), so the section builds the file URI directly instead of blaming the
   owner's database for a panel bug.

## What is not possible, and why

- **The daemon's live conversation cannot be shown.** It is in another process's memory, and no
  endpoint exposes it. Showing it would need a change in the daemon (out of the panel's scope).
- **Scrubbing is pattern-based.** `claire.utils.redact.scrub_secrets` catches vendor-shaped keys,
  `Authorization: Bearer`, keyword-anchored assignments, long hex strings and JWTs. An arbitrary
  secret that matches none of its patterns, typed into the chat box, **is** echoed back. The page
  warns in as many words ("Type no secrets here..."), and a test pins the limitation instead of
  leaving it to be discovered.
- **`sk-proj-...` keys are not matched** by the scrubber's `\bsk-[A-Za-z0-9]{32,}\b`, because the
  second dash breaks the pattern. Noted here rather than fixed: `src/claire/utils/redact.py` sits
  outside the panel's scope.

## The memories browser

The panel's other page is `/memory`, linked from the index card and the footer. It edits the
PostgreSQL memory store one row at a time: the always-loaded router and every stored memory,
memories the router never names, one search (`?q=`) that fuses semantic pgvector retrieval with
keyword retrieval, one memory opened by name (`?note=<name>`), and forms to create, edit and
delete a memory on the page. The persisted daily summaries appear below as a read-only card:
they are summaries, not turns, and the search does not cover them. The page is a browser, not a
second store: it lists, reads, searches and writes through `claire.memory.pg`, so the row
written here is exactly the row Claire's retrieval reads back. **Memory is searchable here but
never leaves the machine**, and it lives in the local PostgreSQL store. The schema, the pgvector
index and the operational runbook are in [PGVECTOR.md](PGVECTOR.md).
