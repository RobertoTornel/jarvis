# Claire Web Panel (R-WEB-1)

A small, **loopback-only** web page so the owner can see, and change, the things that are
otherwise buried in config. One page, server-rendered, standard library only.

## Scope

- Files: `src/claire/panel/__init__.py`, `src/claire/panel/server.py`.
- Tests: `tests/test_panel_server.py`, `tests/test_panel_chat.py`,
  `tests/test_panel_over_a_socket.py`, `tests/test_panel_memory.py`,
  `tests/test_panel_face.py`.
- Entry point: `python -m claire.panel.server` → `http://127.0.0.1:8765/`.
- Owns: rendering the pages and the small set of read routes from **injected state**, the
  provider/model selector form and its write endpoint, the chat form and its endpoint, the
  in-memory turn transcript, the memory editor (`/memory` and its two write endpoints), the
  face read route, and the HTTP guards around them.
- Does **not** own: any of the state it shows. `claire.config` owns provider/model resolution,
  `claire.platform.keys` owns key storage and masking, `claire.memory.pg` owns the PostgreSQL
  memory store and the pgvector search over it, `claire.memory.pg_router` owns the
  always-loaded router read and its budget framing, `claire.memory.embedder` owns embeddings,
  `claire.memory.db` owns the persisted conversation summaries in SQLite, `claire.schedule`
  owns the calendar, `claire.platform.service` owns the self-check, `claire.llm` owns the model
  call. The panel reads them and never edits them: the memory forms call
  `PanelState.save_note` and `PanelState.delete_note`, which write through `claire.memory.pg`,
  so the browser and Claire's retrieval cannot disagree about a stored row.
- No new packages. `http.server`, `html`, `json`, `re`, `dataclasses`, `urllib.parse`,
  `sqlite3`, `threading`, `collections`, `tempfile`, `pathlib` only: no Flask/FastAPI, no
  JavaScript framework, no build step, no template engine. The single inline script on the
  index page is the face poller, which interpolates no data.
- No `__main__.py` in this package (write scope): `python -m claire.panel.server` is the
  documented invocation, and it works because `server.py` uses absolute imports.

## The page

| Section | Source | Shows |
| --- | --- | --- |
| Face | `read_face_state()` | the server-rendered SVG face and its state word, plus the 500 ms poller; the five states are documented in `docs/PANEL_FACE.md` |
| Conversation *(wide)* | `TranscriptBuffer` (+ `RealPanelState.diary()`) | the turns sent through this page, newest first, each labelled you/Claire/panel, with an honest "in memory" note; below it, the persisted daily summaries read-only from `conversation_summaries` |
| Chat with Claire *(wide)* | `PanelState.chat_reply()` | a text box that sends one message through the same LLM stack the daemon uses, and the reply |
| Provider and model | `config.json` via `RealPanelState.provider_model()` | selected provider, resolved model, endpoint, plus the selector form |
| Providers and keys | `claire.platform.keys` | one row per known credential: **masked** value (last-4 + fingerprint) or `absent`, plus its source |
| Memory router | `PanelState.router_status()` (`claire.memory.pg_router`) | the always-loaded router document, `size_bytes` of `budget_bytes` (default 6000), the over/under verdict, and the over-budget warning naming the real size, the budget and the dropped bytes |
| Memories | `PanelState.memory_notes()` (`claire.memory.pg`) | how many memories the store holds, their names/titles/sizes, memories the router document never names, and a link to `/memory` |
| Today's calendar | `claire.schedule` (`load_schedule` + `list_events`) | today's occurrences, formatted through the calendar's own `format_spoken` |
| Self-check | `claire.platform.service.SelfCheck` | summary line, one-click link to the full text report, and every non-READY check with its fix |
| Last actions | action journal (JSONL) | the last 8 records: when, action, outcome, detail |

### The memory editor (`/memory`)

A second server-rendered page, linked from the index card and the footer. It edits the
PostgreSQL memory store one row at a time and never builds a filesystem path from the request.
Query parameters carry the view (`?q=<query>`, `?note=<name>`, `?changed=1`), so every view is
a bookmarkable link.

The page shows:

- the **router** row: the always-loaded document, its byte size against its budget, and its
  body;
- the **memory list**: every stored memory with its name, title and size. A memory the router
  never names is marked rather than hidden;
- **search**: `?q=` runs `claire.memory.pg.search`, which fuses semantic (pgvector) retrieval
  with keyword retrieval and falls back to keyword-only when the embedding backend is down; a
  failed search is a named problem box, not a 500;
- **one memory open**: `?note=<name>` renders its title, body and frontmatter, with the editor
  form and a delete form;
- **new memory**: a create form;
- **persisted diary**: the daily summaries read read-only from SQLite, shown as their own card
  and never written by the panel.

Two forms write, both POST:

| Endpoint | Fields | Behaviour |
| --- | --- | --- |
| `POST /memory/save` | `mode` (`create` or `edit`), `name` (or `original`), `title`, `body` | writes one memory through `PanelState.save_note`, then 303-redirects to `?note=<name>&changed=1`. `create` refuses to clobber an existing name and `edit` refuses to create a new one; an unknown `mode` (`400`), a missing name (`400`), a body longer than `MEMORY_WRITE_MAX_CHARS` (`413`) and a named store failure are each a refusal with its own fix |
| `POST /memory/delete` | `name` | deletes one memory and 303-redirects to `?changed=1`; a missing name is `400`, a store failure is a named refusal, and the router document refuses deletion because it is Claire's always-loaded memory |

Both carry the same origin guard, key-field refusal and body cap as every other write
endpoint, and a refused request writes nothing. A memory name is normalised to a single stored
name (no `/` or `..`) before the write.

### The face

The index page carries one inline SVG face that reflects the assistant's current state. It is
built **server-side**, so the first paint is correct with JavaScript disabled; the one inline
script on the panel, the face poller, then re-reads `GET /face/state` every 500 ms and updates
the face in place without a reload.

The five states are `asleep`, `idle`, `listening`, `thinking` and `speaking`, in the order the
daemon declares them. Each differs in shape and rhythm (closed or open eyes, pupil size, mouth,
animation), never in colour alone, and every motion rule sits inside a
`prefers-reduced-motion: no-preference` query so a still face is equally distinct. The accent
appears only while listening.

The poller reads one file and never writes it; there is no socket or service between the
daemon and the panel. The canonical path is `$XDG_RUNTIME_DIR/claire/claire_state`. That
directory, not `/tmp`, is the channel because `claire.service`, `claire-panel.service` and
`claire-desktop.service` all set `PrivateTmp=yes`, so each unit sees its own private `/tmp`. The
reader tries the shared path first and then `tempfile.gettempdir()/claire_state`, so an older
daemon build or a machine without `XDG_RUNTIME_DIR` still works, and the first candidate that
exists decides. The read is total: a missing, empty, unreadable or unrecognised file reports
`asleep` with `ok: false`, so a failure never guesses a busier state and never breaks the page.
`docs/PANEL_FACE.md` documents the states, the drawing and the manual checks in full.

### The colour law

The panel follows one shared palette, the same one the Android app and the desktop surface
consume: dark grey and light grey carry all structure, and a single pink accent carries
emphasis, the active state and the primary action. Pink is never a status colour, so green,
amber and red keep the success, warning and danger slots.

The panel inlines its tokens in the `:root` block of `_CSS`. Dark mode defines `--bg #131417`,
`--bg-soft` and `--card` `#1b1d21`, `--card-2` `#24262b`, `--ink` `#e8eaed`, `--ink-soft`
`#a8adb5`, `--ink-faint` `#878d96`, `--line` `#31343a`, `--line-soft` `#26282d`, `--accent`
`#e562a4`, `--accent-ink` `#150a12`, `--ok` `#4cc38a`, `--warn` `#d9a441` and `--bad` `#ef6a6a`.
The light theme (`prefers-color-scheme: light`) keeps the palette and sets `--accent` to
`#b93e83` rather than the dark pressed value `#cf4b92`: `#b93e83` measures 4.72:1 as text on
the light background against 3.79:1, clearing the WCAG AA floor of 4.5:1.

In the conversation view, **Claire is pink and the owner is neutral grey**: `.turn.claire`
takes an accent left border and a tint, `.turn.you` a neutral grey border, matching the Android
app. Green means success and never marks who is speaking. The palette is specified in
`docs/MEMORY_EDITOR_UX.md` (§4 the token table, §4.1 the measured contrast ratios), and the
reasoning is in `docs/UI_UX_GUIDE.md`.

## API

Dispatch is two exact-string tables, `GET_ROUTES` and `POST_ROUTES`; there is no path pattern
and no wildcard. `render_health` and `render_face_state` serialise JSON with every `<` replaced
by `\u003c`, so no data value can form a tag. Every response carries `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`.

| Route | Method | Response |
| --- | --- | --- |
| `/` | GET, HEAD | the HTML index, one server-rendered page |
| `/self-check` | GET, HEAD | `text/plain` self-check report, `nosniff` |
| `/healthz` | GET, HEAD | JSON `{panel, ok, summary?, generated_at?, error?}`, `nosniff` |
| `/face/state` | GET, HEAD | JSON `{state, ok, detail?}`, one of the five state names, `Cache-Control: no-store`, `nosniff` |
| `/favicon.ico` | GET, HEAD | `204`, empty `image/x-icon` |
| `/memory` | GET, HEAD | the HTML memory editor; `?q=`, `?note=`, `?changed=1` |
| `/select` | POST | `303` to `/?changed=1` |
| `/chat` | POST | `303` to `/#conversation` |
| `/memory/save` | POST | `303` to `/memory?note=<name>&changed=1` |
| `/memory/delete` | POST | `303` to `/memory?changed=1` |

A GET to a POST route is a `405` naming the method it allows, and a POST to a read route is a
`405` too. An unknown path is a `404 route-not-found` listing the routes that are served, not a
dropped connection. `GET /face/state` is read on every request, so it is never stale.

Those four POST routes are the only write paths. In particular the panel never creates the
diary database, never writes the face state file, and never stores a key.

### The Python surface

| Name | Contract |
| --- | --- |
| `PanelState` | The injectable source: `provider_model`, `key_status`, `config_credentials`, `router_status`, `router_text`, `calendar_today`, `self_check`, `last_actions`, `transcript`, `diary`, `memory_notes`, `memory_note`, `memory_search`, `save_note`, `delete_note`, `chat_reply`, `available_providers`, `available_models`, `select_model`. The reads that have no safe answer raise `PanelStateError`; `PROVIDERS`, `config_credentials()`, `router_text()`, `transcript()`, `diary()`, `memory_notes()`, `memory_search()` and `available_models()` have defaults that say plainly that this state does not read that source, rather than claiming there is nothing there. |
| `RealPanelState` | The real sources. Every path is injectable (`config_path`, `key_store`, `router_path`, `schedule_path`, `action_log_path`, `diary_path`, `vault_dir`, `transcript`, `chat_call`, `environ`, `now`, `self_check_factory`). Reads `config.json` through `get_default_config()` merged over the file and deliberately does **not** run `config._migrate_config` (which persists migrations), so opening a page never changes the owner's files. In Postgres mode the memory and router reads go to `claire.memory.pg`/`pg_router`; `vault_dir` selects the Markdown compatibility path. |
| `TranscriptBuffer` | The bounded, in-process ring of `TurnRecord` (`TRANSCRIPT_LIMIT`, default 50). `turns()` is newest-last; the page renders newest-first. |
| `read_face_state()` | Returns `(state, ok)`: the first existing candidate file that holds one of `FACE_STATES`, else `("asleep", False)`. Total, never raises, one read per request. |
| `PanelLLMError` | The named `502 llm-unavailable` refusal for a chat turn. Wraps `default_chat_call()`, the single seam onto `claire.llm`. |
| `dispatch()` / `render_*` | Route one `PanelRequest` to a `PanelResponse` (`render_index`, `render_memory`, `render_self_check_text`, `render_health`, `render_face_state`, `render_error_page`). `dispatch` never raises: every refusal is a rendered page. |
| `create_handler()` / `create_server()` / `serve()` / `main()` | `create_server` builds the guarded loopback server; `serve` is the only place a socket is opened; `main` is the CLI (`--host`, `--port`, `--allow-remote-binding`, `--action-log`). |

Constants worth knowing: `DEFAULT_BIND_HOST` `127.0.0.1`, `DEFAULT_PORT` `8765`,
`PANEL_VERSION` `1`, `MAX_BODY_BYTES` 64 KiB, `MAX_CHAT_CHARS` 4000,
`CHAT_TIMEOUT_SECONDS` 90, `TRANSCRIPT_LIMIT` 50, `DIARY_LIMIT` 7, `MEMORY_NOTE_LIMIT` 200,
`MEMORY_SEARCH_LIMIT` 25, `MEMORY_BODY_CHARS` 20000, `MEMORY_WRITE_MAX_CHARS` 32 KiB,
`FACE_POLL_MILLISECONDS` 500, and the environment keys `CLAIRE_PANEL_HOST`,
`CLAIRE_PANEL_PORT` and `CLAIRE_ACTION_LOG`.

## Security contract

1. **A raw key value cannot be rendered, structurally.** Every interpolated value passes
   through `_esc` (scrub then HTML escape). The one column that shows credentials is typed as
   `KeyStatus`, whose `render` field must be empty or mask-shaped (`...1234#a1b2c3d4`:
   `is_mask_shaped()` accepts only last-four plus an eight-hex-digit fingerprint, or `****`), or
   construction raises `PanelSecretError`. `KeyStatus.source` is shape-checked too (`store`,
   `env:NAME`, `config.json`), because it is also rendered. Masks come from
   `claire.platform.keys`; the panel never calls `KeyStore.get`, the codebase's single
   raw-value accessor. A credential the panel can only see the *existence* of (a value in
   `config.json` such as `llm_api_key`) is reported as `ConfigCredential` with `present` and no
   value and no mask, because the panel must not read a raw value in order to build a mask of
   it. Every write route (`/select`, `/chat`, `/memory/save`, `/memory/delete`) refuses, by
   name, any posted field in `FORBIDDEN_KEY_FIELDS` (`api_key`, `password`, `token`, `bearer`,
   and friends), so "the panel never accepts a key" holds on routes that do not exist yet too.
   There is deliberately no key field anywhere on the page: keys are set out-of-band, in the key
   store or the provider's environment variable, and the page says so in as many words.

   **No secret is logged.** `PanelHandler.log_message` is overridden so the raw request line
   (which can carry a credential in its query string) never reaches stderr: the panel logs the
   method and the scrubbed, truncated path only, never a form body, a query value or a chat
   message. The chat box is the one place the owner can type a secret, so it is the one place
   the page warns: the text is sent to the model and shown back on the page, scrubbing is
   applied to both message and reply before either is rendered, and the limit of pattern-based
   scrubbing is stated rather than implied away.
2. **Loopback only, by default and by guard.** `DEFAULT_BIND_HOST` is `127.0.0.1`.
   `is_loopback_host()` accepts `127.0.0.0/8`, `::1` and `localhost`, and **not** the empty
   string, because `--host ""` and an `Origin` with no host both mean "somewhere else".
   `CLAIRE_PANEL_HOST` and `CLAIRE_PANEL_PORT` override the defaults. `guard_bind_host()` raises
   `PanelBindError` for anything else unless `allow_remote=True`, and then logs a loud
   `WARNING`: the panel is **plain HTTP**, with no TLS and no authentication, and anyone who can
   reach the interface can read this machine's Claire configuration. `create_server()` runs the
   guard *before* it binds, so a refused host opens no socket.
3. **A cross-origin write is refused, and a refused request writes nothing.** A browser always
   sends `Origin` on a cross-origin form POST, so `check_write_origin()` refuses a write whose
   `Origin` and `Host` disagree on loopback, or whose `Origin` is missing or opaque
   (`Origin: null`) unless `Sec-Fetch-Site: same-origin` accompanies it, which a foreign page
   cannot forge because the browser sets that header from the request's real initiator. Every
   refusal names the route it happened on, because the guard protects more than one endpoint.
   Every write route calls it first: `/select`, `/chat`, `/memory/save`, `/memory/delete`. The
   response referrer policy is deliberately `same-origin` rather than `no-referrer`, because
   `no-referrer` would make the panel's own form POST arrive with `Origin: null` and be refused.
   `/select` then validates the provider against `PanelState.available_providers()` and a
   provider-name pattern, and the model against
   `^[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,127}$`, before `select_model()` is called. `/chat`
   refuses an empty message (`400`) and one longer than `MAX_CHAT_CHARS` (`400`), naming both
   numbers. `/memory/save` refuses an unknown `mode` (`400`), a missing name (`400`) and a body
   longer than `MEMORY_WRITE_MAX_CHARS` (`413`); `/memory/delete` refuses a missing name
   (`400`). Both turn a named memory failure into a refusal with its own fix. A refused request
   writes nothing and calls no model.

   A **body the panel will not read** ends the connection: a chunked request (no
   `Content-Length` at all) and a `Content-Length` that does not parse both set
   `close_connection`. Without that, on an HTTP/1.1 keep-alive connection the unread body bytes
   would be parsed as the *next* request, which is request smuggling inside the loopback panel.
   A body larger than `MAX_BODY_BYTES` is `413 body-too-large`, and a body whose content type is
   not `application/x-www-form-urlencoded` is refused with the reason rather than guessed at.
4. **No directory traversal, because no path is served.** Every route is an exact string in
   the dispatch table. `parse_target` percent-decodes the request target and refuses
   (`400 traversal-refused`) an absolute-form URL, a backslash, a NUL, and any `.`/`..` segment;
   an unknown path is `404 route-not-found`. No filesystem path is ever built from the request,
   and the refusal page (`render_error_page`) deliberately does **not** consult the state, so a
   traversal attempt reads nothing at all. The one route whose *query string* names content is
   `/memory?note=<name>`: a memory is addressed by name, and the store looks it up as a row key
   rather than a path. The write path normalises a name to a single stored name (no `/` or
   `..`) before it writes, and a name the store does not hold is a rendered refusal with a fix.
5. **Nothing is trusted from a source.** Each data section is built inside `_guard`, which
   turns a raised `PanelError` into a named `PROBLEM` box with its fix instead of a 500, and
   turns any other exception into a finding naming its type. A missing file, a dead database,
   an unreadable journal and a broken key store are all rendered as states of the page. The
   face read is the one source that is total by construction: it cannot raise and cannot report
   more activity than the state file proves.

**Known gap (stated, not hidden).** Nothing in this write scope *writes* the action journal:
the panel reads `$CLAIRE_ACTION_LOG` (default `~/.config/claire/actions.jsonl`) and names the
environment variable and the fix when the file is absent. A writer belongs to the component that
owns the actions.

Two further behaviours worth stating rather than discovering:

- **A GET never writes.** `RealPanelState.provider_model()` reads `config.json` with
  `get_default_config()` merged over it and deliberately does **not** run
  `config._migrate_config` (which persists migrations): opening a page must not change the
  owner's files. The writes in this module are `select_model()` (only `llm_provider` and the
  matching model field: `llm_chat_model` for `openai_compatible`, `ollama_chat_model` for
  `ollama`) and the memory editor's create/edit/delete, which exist only as POST forms and go
  through `PanelState.save_note`/`delete_note` and the memory store. Rendering `/memory`,
  opening a memory and searching it never write, and the read-only SQLite URI makes that
  structural for the diary. Key fields are never written, and an unchanged selection is reported
  as unchanged rather than rewritten.
- **Constructing the real key store creates the Claire config directory.** That is
  `KeyStore._ensure_dir()`'s own contract, inherited by `RealPanelState._store()`; it happens
  even when no key exists. The panel adds no files of its own, and the store lives outside the
  repository by design.

**Honest limitation, stated on the page.** The scrubber `claire.utils.redact.scrub_secrets` is
pattern-based. A vendor-shaped or keyword-anchored secret typed into the chat box **is**
redacted before it is rendered; an arbitrary string that matches none of its patterns is echoed
back. The chat hint says so in as many words, and a test pins the limit rather than pretending
it does not exist.

The chat goes through **one** LLM stack, and it is the daemon's own:
`claire.config.load_settings()` → `claire.llm.get_llm_backend(settings)` →
`backend.direct(model, system_prompt, message, ...)`. There is no second HTTP client, no
`requests`, and no `urllib.request` in this module; a test scans the source for both. A panel
that quietly called the endpoint itself would drift from the daemon. `direct()` reports every
failure the same way (no reply, never an exception), so the panel does not guess:
`PanelLLMError` (`502 llm-unavailable`) names the provider, the model and the base URL, says the
backend signals every failure identically, and gives the fix. An empty box is never rendered as
an answer. `chat_reply()` opens the model call under a lock, so two browsers cannot interleave
turns.

A turn is recorded *before* the model call, so a failure is visible in the transcript as a
`note` turn naming the failure, rather than leaving a question that appears never to have been
asked. Turns that a failed call produces are never rendered as Claire's words. The transcript
is bounded (`TRANSCRIPT_LIMIT`) and lives only in this process; the page says so. The persisted
diary is read through a read-only SQLite URI (`file:...?mode=ro`), headed *"summaries, not a
verbatim transcript"*. The panel **never creates that database**: an absent file is reported as
absent, with the path, and the read is never attempted.

## Test obligations

Every panel test file carries `pytestmark = pytest.mark.unit`, because the gate
`pytest tests/ -m unit` silently deselects a file without the marker.

- `tests/test_panel_server.py` pins the security fail-proofs and the page shape: no raw key
  value reaches any route, a mask is last-four plus fingerprint, the rendered page shows only
  the mask and its source, unknown routes and unimplemented sections are named rather than
  blank, the router budget renders quiet under and loud over, the self-check and last-actions
  sections name their failures, the selector form and its write endpoint behave, traversal is
  refused and reads nothing outside, and injected HTML is escaped everywhere.
  Its **escaping** case (`test_html_in_data_values_is_escaped_everywhere`) iterates
  `GET_ROUTES` with an injected `<script>` payload and asserts a **strict count**:
  `response.text.count("<script>") == 1` on the index (the face poller, which interpolates no
  data) and `== 0` on every other HTML page, the same for `</script>`, with the payload rendered as
  `&lt;script&gt;`. Counting rather than banning the substring is the point: injected data can
  never open a script, because the count would move off its fixed number.
  Housekeeping cases also pin the package and module exports, the absence of import-time side
  effects, a single `serve_forever(` call, and that this spec file names the security contract
  (`loopback`, `raw key`, `traversal`, `Origin`, `plain HTTP`, `escaped`).
- `tests/test_panel_face.py` pins the face end to end: `GET /face/state` follows the state
  file, accepts exactly the five names, tolerates a trailing newline, is JSON with
  `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`, refuses a POST with a `405`,
  resolves the shared runtime path before the temp-dir fallback, treats a garbage shared file
  as authoritative, and reads totally (missing, empty, unrecognised, torn and unreadable files
  are all `asleep` with `ok: false` and nothing raises). It also pins the index rendering: the
  current state appears in `data-state`, the accessible `aria-label` and the visible words; the
  page carries exactly one `<script>`, which fetches `/face/state` on a 500 ms interval; every
  face `animation:` declaration sits inside the `prefers-reduced-motion: no-preference` gate;
  and the five states keep distinct shape selectors outside that gate, with `var(--accent)`
  used once, as the listening halo.
- `tests/test_panel_chat.py` pins that chat uses the one LLM stack through
  `get_llm_backend`, that the module opens no provider connection of its own, that a backend
  that refuses or returns nothing is a named error rather than an empty reply, that a chat POST
  303-redirects to the conversation, that the transcript is bounded, newest-first, escaped, and
  labelled as in-memory only, and that the persisted diary is read read-only from the database
  the config points at.
- `tests/test_panel_over_a_socket.py` pins the same routes over a real socket: a real page is
  served, HEAD sends no body, every declared route answers, a GET to `/chat` is a `405`, the
  cross-origin and missing-Origin refusals hold, a model failure is a named `502`, a traversal
  target reads nothing, and a request body the panel will not read closes the connection.
- `tests/test_panel_memory.py` drives `/memory` against an injected memory directory (the suite
  selects the Markdown vault backend through the autouse fixture in `tests/conftest.py`, which
  is the compatibility path). It pins the router and memory listing, opening one memory,
  frontmatter and link resolution, refusing a traversal name, search over the notes and the
  persisted summaries, create/edit/delete round trips through the memory layer, the write
  guards on the two POST routes, and the index card pointing at the browser. The
  store-backed retrieval itself is pinned at the memory layer by
  `tests/test_memory_pg_router.py`.
