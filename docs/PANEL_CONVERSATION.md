# The panel's conversation view, and where conversation data actually lives

Written while adding a transcript and a chat box to the local web panel
(`src/claire/panel/`, `R-WEB-1`). The interesting part of that job was not the HTML — it was
finding out that **there is no retrievable verbatim transcript in this codebase**, and deciding
what to do about it honestly. This note records what was read, what was found, and what the panel
therefore shows.

## What was searched, and what was in each place

| Candidate | Verdict |
| --- | --- |
| `claire.memory.conversation.DialogueMemory` | **Holds the conversation, but only in the daemon process.** `_messages` is a plain `List[Tuple[float, str, str]]` in memory. `get_recent_messages()` / `all_messages()` are callable only from inside that process. |
| A daemon endpoint to read it | **Does not exist.** `grep` for `HTTPServer`, `AF_UNIX`, `socket.socket` and friends outside `panel/` and `phone/` found nothing that serves dialogue. |
| A verbatim transcript on disk | **Does not exist.** Nothing writes an utterance to a file. |
| `conversation_summaries` (SQLite) | **Exists, and is the only persisted record** — per-day *summaries* with `date_utc`, `ts_utc`, `summary`, `topics`, `source_app`. Not turns. On this machine it held a single row. |
| The action journal (`CLAIRE_ACTION_LOG`, `~/.config/claire/actions.jsonl`) | **Not a conversation feed, and nothing writes it.** It is shaped `at/action/outcome/detail` — what the assistant *did*, not what was *said*. `grep` shows only the panel reading it; the default path is absent on this machine. |

So the honest options were: (a) pretend a summary is a transcript, (b) invent a parallel store,
(c) read the daemon's memory — impossible — or (d) show what genuinely exists, labelled with what
it is. The panel does (d).

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
a test scans the source to keep it that way. This project has already been burned once by a
duplicated stack: a phone endpoint was nearly built on a second transcription path while one
authoritative recogniser existed. A panel that quietly called the endpoint itself would drift from
the daemon in exactly the same way.

`direct()` reports every failure identically — it returns `None` and never raises — so the panel
genuinely cannot distinguish "endpoint down" from "model unloaded" from "credential refused".
Rather than guess, `PanelLLMError` (`502 llm-unavailable`) names the provider, the model and the
base URL, and says that the backend signals every failure the same way. An empty box is never
rendered as an answer.

## Three latent bugs found on the way

Each was reproduced before it was fixed, and each has a test that fails without the fix.

1. **Every refusal named `/select`.** `check_write_origin()` hard-coded the route in its message,
   so a refused `POST /chat` blamed a route the owner had not used. Fixed by passing the route in.
2. **An unread request body stayed on a keep-alive connection.** The handler reads a body only
   when `Content-Length` parses. A `Transfer-Encoding: chunked` request has no `Content-Length`,
   so the panel answered `400` and then parsed the body bytes as the *next* request on the same
   connection — request smuggling inside the loopback panel. Invisible to every test that opens a
   fresh connection per request, which was all of them until the socket tests were extended.
   Fixed by closing the connection when a body will not be read.
3. **A relative `db_path` raised out of the diary read.** `Path.as_uri()` rejects relative paths,
   and a config with `"db_path": "claire.db"` is legal — `claire.config` expands `~` and leaves
   the rest alone. The section then blamed the owner's database for a panel bug. Fixed by building
   the file URI directly.

## What is still not possible, and why

- **The daemon's live conversation cannot be shown.** It is in another process's memory, and no
  endpoint exposes it. Showing it would need a change in the daemon (out of the panel's scope).
- **Scrubbing is pattern-based.** `claire.utils.redact.scrub_secrets` catches vendor-shaped keys,
  `Authorization: Bearer`, keyword-anchored assignments, long hex strings and JWTs. An arbitrary
  secret that matches none of its patterns, typed into the chat box, **is** echoed back. The page
  warns in as many words ("Type no secrets here..."), and a test pins the limitation instead of
  leaving it to be discovered.
- **`sk-proj-...` keys are not matched** by the scrubber's `\bsk-[A-Za-z0-9]{32,}\b`, because the
  second dash breaks the pattern. Noted here rather than fixed: `src/claire/utils/redact.py` is
  outside this change's scope.
