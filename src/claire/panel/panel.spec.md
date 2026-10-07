# Claire Web Panel (R-WEB-1)

A small, **loopback-only** web page so the owner can see — and change — the things that are
otherwise buried in config. One page, server-rendered, standard library only.

## Scope

- Files: `src/claire/panel/__init__.py`, `src/claire/panel/server.py`.
- Tests: `tests/test_panel_server.py`, `tests/test_panel_chat.py`,
  `tests/test_panel_over_a_socket.py`.
- Entry point: `python -m claire.panel.server` → `http://127.0.0.1:8765/`.
- Owns: rendering one HTML page and a tiny set of read routes from **injected state**, the
  provider/model selector form and its write endpoint, the chat form and its endpoint, the
  in-memory turn transcript, and the HTTP guards around them.
- Does **not** own: any of the state it shows. `claire.config` owns provider/model resolution,
  `claire.platform.keys` owns key storage and masking, `claire.memory.router` owns `CLAIRE.md`
  and its budget framing, `claire.schedule` owns the calendar, `claire.platform.service` owns
  the self-check, `claire.llm` owns the model call, `claire.memory.db` owns the persisted
  conversation summaries. The panel reads them and never edits them.
- No new packages. `http.server`, `html`, `json`, `re`, `dataclasses`, `urllib.parse`, `sqlite3`,
  `threading`, `collections` only — no Flask/FastAPI, no JavaScript, no build step, no template
  engine.
- No `__main__.py` in this package (write scope): `python -m claire.panel.server` is the
  documented invocation, and it works because `server.py` uses absolute imports.

## The page

| Section | Source | Shows |
| --- | --- | --- |
| Conversation *(wide)* | `TranscriptBuffer` (+ `RealPanelState.diary()`) | the turns sent through this page, newest first, each labelled you/Claire/panel, with an honest "in memory" note; below it, the persisted daily summaries read-only from `conversation_summaries` |
| Chat with Claire *(wide)* | `PanelState.chat_reply()` | a text box that sends one message through the same LLM stack the daemon uses, and the reply |
| Provider and model | `config.json` via `RealPanelState.provider_model()` | selected provider, resolved model, endpoint, plus the selector form |
| Providers and keys | `claire.platform.keys` | one row per known credential: **masked** value (last-4 + fingerprint) or `absent`, plus its source |
| Memory router | `claire.memory.router` | `CLAIRE.md` path, `size_bytes` of `budget_bytes` (default 6000), over/under verdict, and the **verbatim** over-budget warning the prompt uses |
| Today's calendar | `claire.schedule` (`load_schedule` + `list_events`) | today's occurrences, formatted through the calendar's own `format_spoken` |
| Self-check | `claire.platform.service.SelfCheck` | summary line, one-click link to the full text report, and every non-READY check with its fix |
| Last actions | action journal (JSONL) | the last 8 records: when, action, outcome, detail |

Layout is a CSS-grid masthead plus `main` (`repeat(auto-fit, minmax(330px, 1fr))`); the
conversation and chat cards span every column (`.wide`), and `@media (max-width: 560px)` drops to
one column. The palette is one set of CSS custom properties on `:root`, dark by default with
`@media (prefers-color-scheme: light)` overrides, so there is no JS and no build step anywhere in
the styling. `.card-body` is `overflow-x: auto` — without it a wide table (the self-check's) was
clipped and its right-hand columns were unreachable in a narrow window.

Routes (all exact strings; `GET_ROUTES` and `POST_ROUTES` are the complete lists and tests
iterate them):

| Route | Method | Response |
| --- | --- | --- |
| `/` | GET | `200` HTML — the page |
| `/self-check` | GET | `200` `text/plain` — the self-check report |
| `/healthz` | GET | `200` `application/json` — `{panel, ok, summary, generated_at}` |
| `/favicon.ico` | GET | `204` |
| `/select` | POST | `303` to `/?changed=1` on success; a refusal page otherwise |
| `/chat` | POST | `303` to `/#conversation` on success; a refusal page otherwise |

`GET /chat` is `405` (`method-not-allowed`): the route exists only as a form target.

The body of every rendered page is HTML. Content that came from the internet (calendar titles
and notes, action details, model ids, error text, the router body, **the owner's chat message and
the model's reply, and every diary summary**) is **escaped**: every interpolated value passes
through `_esc()`, which runs `claire.utils.redact.scrub_secrets()` and then
`html.escape(..., quote=True)`. HTML in a data value is data, never markup.

## Chat, and where the conversation actually comes from

**The source-of-truth finding, stated plainly, because it shapes the design.** There is no
retrievable verbatim transcript in this codebase. `claire.memory.conversation.DialogueMemory`
holds the live conversation in `_messages`, a plain list in the **daemon process**, and the daemon
exposes no socket or HTTP endpoint to read it; nothing writes an utterance to disk. The only
persisted record is the `conversation_summaries` table (per-day **summaries**, not turns). The
action journal is not a conversation feed either: it stores `at/action/outcome/detail` records,
not speech, and nothing in the codebase writes it at all. So the panel does not invent a
parallel store and does not pretend a summary is a transcript. It shows two things, each labelled
with what it actually is:

1. **This page's own turns** — a bounded, thread-safe `deque` (`TranscriptBuffer`,
   `TRANSCRIPT_LIMIT`), populated by the chat form and by `RealPanelState.chat_reply()`. Labelled
   `in memory` / `durable=False`, with the note that says the turns are lost when the panel
   restarts and that Claire's live voice conversation is inside the daemon, unreadable from here.
2. **The persisted diary** — the `conversation_summaries` rows for the last `DIARY_LIMIT` days,
   read through a read-only SQLite URI (`file:...?mode=ro`), headed *"summaries, not a verbatim
   transcript"*. The panel **never creates the database**: an absent file is reported as absent,
   with the path, and the read is never attempted.

A turn is recorded *before* the model call, so a failure is visible in the transcript as a `note`
turn naming the failure, rather than leaving a question that appears never to have been asked.
Turns that a failed call produces are never rendered as Claire's words.

The chat goes through **one** LLM stack, and it is the daemon's own:
`claire.config.load_settings()` → `claire.llm.get_llm_backend(settings)` →
`backend.direct(model, system_prompt, message, ...)`. There is no second HTTP client, no
`requests`, and no `urllib.request` in this module — a test scans the source for both. The
humourless reason: this project has already been burned once by a duplicated stack (a phone
endpoint nearly built on a second transcription path while one authoritative recogniser existed),
and a panel that quietly called the endpoint itself would drift from the daemon exactly as that
did.

`direct()` reports every failure the same way — no reply, never an exception — so the panel
cannot tell "the endpoint is down" from "the model is unloaded" from "the credential was
refused". It does not guess: `PanelLLMError` (`502 llm-unavailable`) names the provider, the
model and the base URL, says the backend signals every failure identically, and gives the fix.
An empty box is never rendered as an answer.

`chat_reply()` opens the model call under a lock, so two browsers cannot interleave turns.

**Honest limitation, stated on the page.** The scrubber `claire.utils.redact.scrub_secrets` is
pattern-based. A vendor-shaped or keyword-anchored secret typed into the chat box **is** redacted
before it is rendered; an arbitrary string that matches none of its patterns is echoed back. The
chat hint says so in as many words ("Type no secrets here: this text is sent to the model and
shown back on this page"), and a deliberately-named tripwire test pins the limitation rather than
letting it be discovered.

## Security contract

1. **A raw key cannot be rendered — structurally, not by review.**
   - The panel calls `KeyStore.render()` (masked) and never `KeyStore.get()` — the codebase's
     single raw-value accessor, named by `claire.platform.keys.RAW_VALUE_ACCESSOR`. The
     fail-proof test is behavioural: `KeyStore.get` is patched to raise and every route must
     still render, with the mask present and the raw value absent.
   - `KeyStatus.render` is the only credential-bearing field on a page. Its `__post_init__`
     refuses any non-empty value that is not **exactly** the shape
     `claire.platform.keys.mask()` produces (`...1234#a1b2c3d4`, or `****#a1b2c3d4` for a short
     value): `PanelSecretError` is raised instead. `KeyStatus.source` is shape-checked too
     (`store`, `env:NAME`, `config.json`), because it is also rendered.
   - A credential the panel can only see the *existence* of (a value in `config.json` such as
     `llm_api_key`) is reported as `ConfigCredential` — `present`, no value, no mask — because
     the panel must not read a raw value in order to build a mask of it.
   - **The page has no key field**, and both write routes — `POST /select` and `POST /chat` —
      refuse any body carrying one (see 3). Keys are set out-of-band: the key store, or the
      provider's environment variable (`OMNIROUTE_API_KEY`). The page says so in as many words.
   - **No secret is logged.** `PanelHandler.log_message` is overridden so the raw request line
     (which can carry a credential in its query string) is never written to stderr: the panel
     logs the method and the *path* only, scrubbed and truncated. The panel never logs a form
     body, a query value, or a chat message.
   - The chat box is the one place the owner can type a secret, so it is the one place the page
     warns: the text is sent to the model and shown back on the page. Scrubbing is applied to
     both the message and the reply before either is rendered, and the limit of pattern-based
     scrubbing is stated on the page rather than implied away.
2. **Loopback only, by default and by guard.** `DEFAULT_BIND_HOST` is `127.0.0.1`.
   `is_loopback_host()` accepts `127.0.0.0/8`, `::1` and `localhost` — and **not** the empty
   string, because `--host ""` and an `Origin` with no host both mean "somewhere else".
   `guard_bind_host()` raises `PanelBindError` for anything else unless `allow_remote=True`, and
   then logs a loud `WARNING`: the panel is **plain HTTP — no TLS, no authentication**, readable
   by anyone who can reach the interface. `create_server()` runs the guard *before* it binds, so
   a refused host never opens a socket.
3. **Both write endpoints are forgery-resistant.** `check_write_origin()` runs before the body is
   looked at, for `POST /select` and `POST /chat` alike, and refuses with `PanelOriginError`
   (`403 origin-refused`) unless:
   - `Origin` is present (silence is not a same-origin proof),
   - its scheme is `http`,
   - its host is loopback,
   - its host equals the `Host` header's host,
   - any port on either header equals the port the panel is running on,
   - `Sec-Fetch-Site`, when sent, is `same-origin` or `none`.

   Every refusal message names the route it refused (`POST /chat was refused: ...`). It did not
   always: the guard hard-coded `/select` in its wording, so a refused chat POST blamed a route
   the owner had not used — a latent bug, caught by a test and fixed. The body is then parsed as
   `application/x-www-form-urlencoded`, capped at `MAX_BODY_BYTES` (64 KiB,
   `413 body-too-large`), and refused (`403 origin-refused`) if it carries any name in
   `FORBIDDEN_KEY_FIELDS`. `/select` validates provider and model against
   `PanelState.available_providers()` and a conservative model-id pattern
   (`^[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,127}$`) before `select_model()` is called; `/chat`
   refuses an empty message (`400`) and one longer than `MAX_CHAT_CHARS` (`400`), naming both
   numbers. A refused request writes nothing and calls no model.

   A **body the panel will not read** ends the connection: a chunked request (no
   `Content-Length` at all) and a `Content-Length` that does not parse both set
   `close_connection`. Without that, on an HTTP/1.1 keep-alive connection the unread body bytes
   would be parsed as the *next* request — request smuggling inside the loopback panel. That was
   a real latent bug, caught over a socket and fixed.
4. **No directory traversal, because no path is served.** Every route is an exact string in
   the dispatch table. `parse_target()` percent-decodes the request target and
   refuses (`400 traversal-refused`) an absolute-form URL, a backslash, a NUL, and any `.`/`..`
   segment; an unknown path is `404 route-not-found`. No filesystem path is ever built from the
   request, and the refusal page (`render_error_page`) deliberately does **not** consult the
   state, so a traversal attempt reads nothing at all — not even `CLAIRE.md`.
5. **Every missing thing is a named error with a fix; nothing raises past the handler.**
   - HTTP-level refusals render `PanelError.code`, the message and `fix` on a standalone page.
   - A *section* whose state provider raises is caught by `_guard()` and rendered as a named
     PROBLEM box inside the page — never a 500 with a traceback. `PanelState` raises
     `PanelStateError` for sections it does not implement, so a partial fake is safe and an
     unimplemented section is visible rather than blank.
   - A key store that exists but cannot be read (`KeyStoreCorruptError`, `OSError`) is
     `PanelSourceError` (`500 source-unreadable`), never "no keys" — the difference between
     "you have no keys" and "I could not read your keys" matters to a person deciding whether
     to trust the answer.
   - The action journal's absence is named (`no action journal at <path>`) with the fix that
     names `$CLAIRE_ACTION_LOG`. Unparseable journal lines are **counted and shown**, never
     silently skipped.

**Known gap (stated, not hidden).** Nothing in this write scope *writes* the action journal:
the panel reads `$CLAIRE_ACTION_LOG` (default `~/.config/claire/actions.jsonl`) and says so,
with the fix, when the file is absent. A writer belongs to the component that owns the actions.

Two further behaviours worth stating rather than discovering:

- **A GET never writes.** `RealPanelState.provider_model()` reads `config.json` with
  `get_default_config()` merged over it and deliberately does **not** run `config._migrate_config`
  (which persists migrations): opening a page must not change the owner's files. The one write in
  this module is `select_model()`, and it touches only `llm_provider` and the matching model field
  (`llm_chat_model` for `openai_compatible`, `ollama_chat_model` for `ollama`). Key fields are
  never written, and an unchanged selection is reported as unchanged rather than rewritten.
- **Constructing the real key store creates the Claire config directory.** That is
  `KeyStore._ensure_dir()`'s own contract, inherited by `RealPanelState._store()`; it happens even
  when no key exists. The panel adds no files of its own, and the store lives outside the
  repository by design.

## API

| Name | Contract |
| --- | --- |
| `PanelState` | The injectable source: `provider_model`, `key_status`, `config_credentials`, `router_status`, `calendar_today`, `self_check`, `last_actions`, `transcript`, `diary`, `chat_reply`, `available_providers`, `available_models`, `select_model`. The base class implements none of the sections (they raise `PanelStateError`); `PROVIDERS` and `config_credentials()` have defaults. |
| `RealPanelState` | The real sources. Every path is injectable (`config_path`, `key_store`, `router_path`, `schedule_path`, `action_log_path`, `diary_path`, `transcript`, `chat_call`, `environ`, `now`, `self_check_factory`). Reads `CLAIRE.md` with `create=False` — the panel never creates a memory file behind the owner's back. Reads the diary through a read-only SQLite URI and never creates it. Caches the self-check for `SELF_CHECK_TTL_SECONDS` (30). |
| `TranscriptBuffer` | A bounded, lock-guarded `deque` of `TurnRecord`; `record`, `turns` (oldest first), `clear`, `limit`. |
| `TurnRecord` | One turn: `role` in `TURN_ROLES` (`you`/`claire`/`note`), scrubbed `text` and `at`. An unknown role becomes `note` rather than being rendered as Claire. |
| `TranscriptStatus` | `turns`, `source`, `durable`, `note`, `error`, `fix`. `durable=False` is what the page's "in memory" badge reports. |
| `DiaryEntry` / `DiaryStatus` | One persisted summary (date, recorded time, summary, topics, source app) and the read's outcome, including the path and why it failed. |
| `ChatResult` | `text`, `provider`, `model`, `base_url`, `elapsed_ms` — what one successful chat turn produced. |
| `PanelLLMError` | `502 llm-unavailable`: names provider, model and base URL, and states that the backend reports every failure identically. |
| `default_chat_call(message, *, timeout_sec, settings=None)` | The **one** place the panel calls a model: `load_settings()` → `get_llm_backend()` → `backend.direct(...)`. No second client. |
| `handle_chat`, `handle_select` | The two write handlers; both run `check_write_origin(route=...)` first. |
| `PanelRequest` | `method`, `path`, `headers` (case-insensitive), `body`, `client`, `server_port`, `server_host`, `declared_length`. Buildable with no socket. |
| `PanelResponse` | `status`, `body` (bytes), `content_type`, `headers`. |
| `dispatch(state, request)` | Route one request. **Never raises**: every refusal is a rendered `PanelResponse`. |
| `render_index(state, *, changed=False, ...)` | The HTML page. |
| `render_self_check_text(state)` / `render_health(state)` | The text and JSON views. |
| `render_error_page(exc)` | A standalone refusal page; touches no state. |
| `create_handler(state)` | A `PanelHandler` subclass bound to `state`; starts nothing. |
| `create_server(state, host, port, *, allow_remote=False)` | Builds (does not start) the server; guards the host first. |
| `serve(state, host, port, *, allow_remote=False)` | The only place a socket is opened. |
| `main(argv)` | CLI: `--host`, `--port`, `--allow-remote-binding`, `--action-log`. Returns an exit code. |
| `guard_bind_host`, `is_loopback_host`, `is_mask_shaped`, `parse_target`, `parse_form`, `check_write_origin` | The guards, individually testable. |

Constants: `ROUTE_CHAT`, `POST_ROUTES`, `GET_ROUTES`, `MAX_CHAT_CHARS` (4000),
`CHAT_TIMEOUT_SECONDS` (90), `TRANSCRIPT_LIMIT` (50), `DIARY_LIMIT` (7),
`PANEL_CHAT_SYSTEM_PROMPT`.

## Test obligations

`tests/test_panel_server.py` carries `pytestmark = pytest.mark.unit` (the gate silently
deselects an unmarked file) and starts **no server** — it builds requests and calls `dispatch`:

- every route in `GET_ROUTES` renders `200` with injected fake state;
- a raw key value never appears in the HTML of **any** route, and never in a log line;
- `KeyStore.get` patched to raise: every route still renders and the mask is still shown;
- `KeyStatus(render=<raw>)` raises `PanelSecretError`, and `is_mask_shaped(mask(v))` is true;
- the router budget renders correctly both under and over budget, with the loud framing;
- the model selector form is present on `/`, and a valid POST writes and redirects;
- a POST with no `Origin`, a cross-origin `Origin`, a mismatched `Host`, or a key-shaped field
  is refused and writes nothing;
- `'../etc/passwd'`-style paths (raw and percent-encoded) are refused, and no file outside the
  injected roots is opened (recorded via a patched `builtins.open`);
- a `<script>` in any data value is escaped, on every route;
- `DEFAULT_BIND_HOST` is loopback, and a non-loopback host is refused without binding;
- escaping, the `-m unit` marker guard, and the module's own `__all__` stay honest.

`tests/test_panel_chat.py` (in-process, no socket) adds:

- the chat seam: `handle_chat` resolves the model through `get_llm_backend`, and the module's
  source contains neither `requests` nor `urllib.request`;
- a reply that is `None` or blank is a named `502 llm-unavailable`, never an empty answer;
- the turns: the owner's turn is recorded before the call, a failed call is recorded as a `note`
  and never as Claire's words, the buffer is bounded and newest-first, and the page says plainly
  that the transcript is in memory only;
- the diary: read from the config's `db_path`, rendered, escaped, newest-first and bounded by
  `DIARY_LIMIT`; the database is **never created** (the read leaves the file byte-identical); a
  missing table and a corrupt file are each a *named* problem; a relative `db_path` is a finding,
  not an exception;
- the origin guard names the route it refused (`POST /chat was refused`, not `/select`);
- the scrubbing that works, and — in a deliberately-named tripwire test — the pattern-matching
  limit that does not.

`tests/test_panel_over_a_socket.py` starts the real `ThreadingHTTPServer` on an ephemeral port,
because in-process tests structurally cannot cover wire behaviour. It adds:

- a chat POST end to end: a real urlencoded body in, a `303` to `/#conversation` out, and the
  panel's own redirect followed to a page showing both turns;
- turns accumulating across requests in one panel process, and surviving a failed turn;
- `GET /chat` → `405`; a cross-origin chat POST → `403` **naming `/chat`**, and never reaching the
  model; a body carrying a key field → `403` with no key on the page;
- an opaque `Origin: null` accepted only with `Sec-Fetch-Site: same-origin`;
- a `HEAD` that carries no body but still describes its length;
- every route in `GET_ROUTES`/`POST_ROUTES` answering, and a traversal target reading nothing;
- the fix for a latent HTTP bug: a body the panel will not read (chunked, or an unparseable
  `Content-Length`) leaves the connection **closed**, so those bytes can never be parsed as the
  next request on a keep-alive connection.
