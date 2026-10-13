# Web panel

**Authoritative spec: `src/claire/panel/panel.spec.md` (R-WEB-1). Read this when you are changing the panel's routes, its server-rendered page, its write guards, or the config keys it reads.**

*Anchor note. Line numbers point into `src/claire/panel/server.py` as it currently stands (about 4,900 lines). Treat the named symbol as the stable anchor and the number as a pointer; the file grows with each new route.*

## Responsibility

`src/claire/panel/` is a small, **loopback-only** web page plus a handful of read and write routes, so the owner can see — and change — the things that are otherwise buried in config. One page, server-rendered, standard library only (`http.server`, `html`, `json`, `re`, `dataclasses`, `urllib.parse`, `sqlite3`, `threading`, `collections`): no Flask/FastAPI, no build step, no template engine, and one small inline script, the face poller on the index, which is the only script the panel serves.

It owns rendering and the HTTP guards around it. It owns **none of the state it shows**: `claire.config` owns provider/model resolution, `claire.platform.keys` owns key storage and masking, `claire.memory.pg` owns the PostgreSQL memory store and the pgvector search over it, `claire.memory.pg_router` owns the always-loaded router read, `claire.memory.embedder` owns embeddings, `claire.schedule` owns the calendar, `claire.platform.service` owns the self-check, `claire.llm` owns the model call, and `claire.memory.db` owns the persisted conversation summaries. The panel reads them, and writes memory only through `PanelState.save_note` and `PanelState.delete_note`, which write through `claire.memory.pg`.

## Key modules

| Symbol | Location | What it is |
| --- | --- | --- |
| `PanelRequest` / `PanelResponse` | `src/claire/panel/server.py:1068`, `:1101` | One request and one rendered response; both socket-free, which is what lets the whole request path be tested without a server. |
| `PanelState` | `src/claire/panel/server.py:1444` | The injectable source interface every route takes. |
| `RealPanelState` | `src/claire/panel/server.py:1656` | The real sources: `config.json`, the key store, the PostgreSQL memory store, the schedule, the self-check and the action journal. |
| `TranscriptBuffer` | `src/claire/panel/server.py:1340` | A bounded, thread-safe ring buffer of turns sent through this page. Not a transcript of the daemon. |
| `default_chat_call` | `src/claire/panel/server.py:1388` | The one LLM path: `claire.config.load_settings` plus `claire.llm.get_llm_backend`. |
| `parse_target` | `src/claire/panel/server.py:1162` | Normalises the request target and refuses traversal by name. |
| `check_write_origin` | `src/claire/panel/server.py:1197` | The CSRF guard on every write endpoint. |
| `parse_form` | `src/claire/panel/server.py:1302` | Parses `application/x-www-form-urlencoded` under the body cap. |
| `dispatch` | `src/claire/panel/server.py:4643` | Routes one request; never raises, every refusal is a rendered page. |
| `PanelHandler` | `src/claire/panel/server.py:4759` | The `BaseHTTPRequestHandler` adapter. Holds no policy. |
| `create_handler` / `create_server` / `serve` | `src/claire/panel/server.py:4834`, `:4846`, `:4858` | Build the handler bound to one state, build the server, serve it. |
| `main` | `src/claire/panel/server.py:4892` | CLI: `python -m claire.panel.server`. |

## HTTP routes

`GET_ROUTES` (`src/claire/panel/server.py:209`) and `POST_ROUTES` (`:220`) are the two halves of the route surface; `dispatch` (`:4643`) is what enforces them. A `GET` to a POST route is a named `405`, never a silent no-op.

| Method + path | Handler | What it does |
| --- | --- | --- |
| `GET`/`HEAD` `/` | `render_index` (`:4268`) | The one HTML page: the face, conversation, chat, provider/model selector, providers and keys, memory router, memories, today's calendar, self-check, last actions. `?changed=1` marks a just-saved selection. |
| `GET`/`HEAD` `/self-check` | `render_self_check_text` (`:4301`) | The self-check report as `text/plain`, so "is anything broken" is one click. |
| `GET`/`HEAD` `/healthz` | `render_health` (`:4329`) | `application/json` `{panel, ok, summary, generated_at}` (plus `error` when a probe fails). |
| `GET`/`HEAD` `/face/state` | `render_face_state` (`:4351`) | `application/json` `{state, ok, detail?}`, one of the five state names, `Cache-Control: no-store`. The index's one script polls it every 500 ms. |
| `GET`/`HEAD` `/favicon.ico` | inline in `dispatch` (`:4664`) | `204`, empty. |
| `GET`/`HEAD` `/memory` | `render_memory` (`:3935`) | The memories browser: the router, the note list, note search and one open note. `?q=` searches, `?note=` opens a note, `?changed=1` marks a saved note. Dispatched by name rather than through `GET_VIEWS` because its view takes the query string. |
| `POST` `/select` | `handle_select` (`:4478`) | Validates provider/model, writes the selection, redirects `303` to `/?changed=1`. |
| `POST` `/chat` | `handle_chat` (`:4452`) | One model turn through the shared LLM stack, redirects `303` to `/#conversation` so a refresh cannot re-send. |
| `POST` `/memory/save` | `handle_memory_save` (`:4574`) | Creates or replaces one note. `mode` is explicit (`create` must refuse to clobber, `edit` must refuse to create). |
| `POST` `/memory/delete` | `handle_memory_delete` (`:4622`) | Deletes one memory through `claire.memory.pg`. |

`GET_VIEWS` (`src/claire/panel/server.py:4412`) maps `/`, `/self-check`, `/healthz` and `/face/state` to view functions. `/memory` is dispatched by name because, like `/`, its view takes arguments.

## UI and serving model

- **One page, rendered by string concatenation.** `render_index` (`:4268`) joins the masthead, the section helpers (`_conversation_section` `:3550`, `_chat_section`, `_provider_section`, `_keys_section`, `_router_section`, `_memory_section` `:3636`, `_calendar_section`, `_self_check_section`, `_actions_section`) and a footer, then appends the one inline script the panel serves, the face poller. The styling is one set of CSS custom properties on `:root`, dark by default with a `prefers-color-scheme: light` override, and a grid layout with wide conversation/chat cards; the styling itself carries no script and no build step.
- **Every interpolated value is escaped.** `_esc` (`src/claire/panel/server.py:614`) runs `claire.utils.redact.scrub_secrets()` and then `html.escape(..., quote=True)`. HTML that came from the internet (calendar titles, action details, model ids, error text, the owner's chat message, the model's reply, diary summaries, note names and bodies) is data, never markup.
- **Responses are plain HTTP/1.1** (`PanelHandler.protocol_version`, `src/claire/panel/server.py:4770` region) with accurate `Content-Length`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and a `same-origin` referrer policy — the last one deliberately, because `no-referrer` made the browser send an opaque `Origin`, which the write guard then correctly refused.
- **Bodies are read from the declared length only.** `_request_from_handler` (`:4711`) reads exactly `Content-Length` bytes, capped at `MAX_BODY_BYTES` (`:170`, 64 KiB). A chunked request (`Transfer-Encoding`, no length) or an unparsable length reads nothing and sets `close_connection`, so unread body bytes cannot be parsed as the next request on a keep-alive connection. A body over the cap is a `413` and the connection closes.
- **Refusals are rendered, not dumped.** `render_error_page` (`:4379`) consults no state on purpose — a refused request must not cause reads of the owner's files — and shows the error's code, message and fix.

## How it reaches daemon state, and what it cannot reach

The panel is a **separate process with no IPC to the daemon or the listener**. Nothing outside `src/claire/panel/` imports it, and the daemon does not start it: the only entry point is `python -m claire.panel.server` (the `__main__` guard at the end of `server.py`). The repo-root `start.sh` (`start.sh:534`) runs it as its own process beside the daemon, reading `CLAIRE_PANEL_HOST` / `CLAIRE_PANEL_PORT` (`start.sh:431`, `:437`) but never pointing it at a daemon socket, because there is none to point at.

What it does instead is call the same in-process libraries the daemon uses:

- **Chat** goes through `default_chat_call` (`src/claire/panel/server.py:1388`): `claire.config.load_settings()` resolves provider, model, endpoint and credential, and `claire.llm.get_llm_backend(settings)` returns the backend object the daemon and the reply engine use. It deliberately opens no HTTP connection of its own.
- **Keys** are shown through `KeyStore.render()` (masked). The panel never calls `KeyStore.get()`, the codebase's single raw-value accessor.
- **Memory router** reads the always-loaded router from `claire.memory.pg_router`, so the panel never creates a memory row behind the owner's back.
- **Diary** reads the `conversation_summaries` table read-only. It never creates the database.
- **Calendar** goes through `claire.schedule` (`load_schedule` + `list_events`), formatted with the calendar's own `format_spoken`.
- **Self-check** goes through `claire.platform.service.SelfCheck`, cached for `SELF_CHECK_TTL_SECONDS` (30 s, `RealPanelState.SELF_CHECK_TTL_SECONDS`).
- **Memories** go through `claire.memory.pg`; the panel never formats frontmatter itself, so the browser writes exactly what Claire's retrieval reads back.

The consequence is stated on the page rather than papered over: the daemon's live conversation (`claire.memory.conversation.DialogueMemory`) is a plain list in the **daemon process** with no socket or HTTP endpoint to read it, and the panel holds no reference to the `VoiceListener`. Its conversation card therefore shows only the turns sent through this panel, held in `TranscriptBuffer` (`:1340`), labelled "in memory" and `durable=False`, alongside the persisted daily summaries, which are summaries and not turns. This finding is recorded in `docs/PANEL_CONVERSATION.md`.

## Auth and exposure

- **There is no authentication and no TLS.** The security boundary is loopback. `DEFAULT_BIND_HOST` is `127.0.0.1` (`src/claire/panel/server.py:161`) and `DEFAULT_PORT` is 8765 (`:162`).
- `guard_bind_host` (`:1138`) refuses any non-loopback host with `PanelBindError` (`:499`) unless `allow_remote=True`; when the operator opts in it logs a loud warning in plain words, and `serve` (`:4858`) names the bound host "REACHABLE FROM THE NETWORK" rather than claiming loopback.
- **Writes are CSRF-guarded.** `check_write_origin` (`:1197`) requires an `http`-scheme `Origin` whose host is loopback and matches the `Host` header, with matching ports, and `Sec-Fetch-Site` of `same-origin`, `none` or empty when that header is sent. A missing or opaque (`null`) `Origin` is accepted only alongside `Sec-Fetch-Site: same-origin` — a header a foreign page cannot forge — and refused otherwise, so silence alone is never a same-origin proof. Every refusal names the route it refused.
- **Every write refuses credential-shaped field names** through `refuse_key_fields` (`:4429`), which uses `FORBIDDEN_KEY_FIELDS` (`:174`). The panel has no key field anywhere; a POST that carries one is refused by name rather than ignored, so the rule holds on routes that do not exist yet.
- **No raw secret can be rendered.** Credential-bearing fields are mask-shaped values (last four characters plus a fingerprint) or presence-only notices; `RealPanelState.CONFIG_CREDENTIAL_FIELDS` names the config keys whose *presence* is reported, never their values.
- **Logs are scrubbed.** `PanelHandler.log_message` logs the method and the scrubbed path only, never the query string or a header, because a credential can arrive in a query string.

## Configuration keys

| Key | Where | Meaning |
| --- | --- | --- |
| `--host` | `src/claire/panel/server.py:4901` | Bind host; default the panel's `DEFAULT_BIND_HOST` (127.0.0.1). |
| `--port` | `src/claire/panel/server.py:4906` | Bind port; default `DEFAULT_PORT` (8765). |
| `--allow-remote-binding` | `src/claire/panel/server.py:4912` | Accepts a non-loopback bind; prints the plain-HTTP warning. |
| `--action-log` | `src/claire/panel/server.py:4916` | Action journal path; falls back to `$CLAIRE_ACTION_LOG`. |
| `CLAIRE_ACTION_LOG` | `src/claire/panel/server.py:332` | The action journal path; default `~/.config/claire/actions.jsonl` (`DEFAULT_ACTION_LOG`, `:333`). |
| `CLAIRE_PANEL_HOST`, `CLAIRE_PANEL_PORT` | `src/claire/panel/server.py:341`, `:342` | Bind overrides; the panel reads them as the defaults for `--host`/`--port` (`:4902`, `:4908`), and `start.sh` reads the same two variables (`start.sh:431`, `:437`) and passes them as flags (`start.sh:595-596`). |
| `llm_provider`, `llm_base_url`, `llm_chat_model`, `ollama_base_url`, `ollama_chat_model` | `src/claire/config.py` | Read through `claire.config` for the provider/model card and for chat. |
| `vault_dir` | `src/claire/config.py:123`, `:647`, `:994-995` | The Markdown compatibility path, selected by `memory_backend: vault`; default `~/.local/share/claire/vault`. Overridable per run by `CLAIRE_VAULT_DIR` (`src/claire/memory/notes.py:81`), resolved by `notes.resolve_vault_dir`. |
| `llm_api_key`, `embedding_api_key`, `brave_search_api_key` | `src/claire/panel/server.py:1674` | Not read for value: `RealPanelState` reports only whether each is present. Values live in the OS keychain or a `0600` file outside the repository. |

## Data and control flow

One request, end to end:

1. `PanelHandler.do_GET`/`do_POST` builds a `PanelRequest` (`_request_from_handler`, `src/claire/panel/server.py:4711`) and calls `dispatch` (`:4643`). Tests skip the socket and call `dispatch` with a hand-built request.
2. `dispatch` normalises the target with `parse_target` (`:1162`), which percent-decodes and refuses an absolute-form URL, a backslash, an embedded NUL and any `.`/`..` segment by name (400 `traversal-refused`). No path from a request is ever used to open a file.
3. POST routes run the guards in order: `check_write_origin` (`:1197`), `parse_form` (`:1302`) under the 64 KiB cap, then `refuse_key_fields` (`:4429`), then the handler's own validation.
4. The handler reads or writes through the injected `PanelState` and returns a `PanelResponse`. A raised `PanelError` is caught by `dispatch` and rendered by `render_error_page` (`:4379`).
5. State touched by the writes: `/select` validates the provider against `PanelState.available_providers()` and the model against the conservative `_MODEL_RE` pattern before `select_model`; `/chat` records the owner's turn *before* the model call, then calls `chat_reply` under a lock and records either the reply or a named "no reply" note; `/memory/save` writes through `PanelState.save_note` and re-reads the stored memory; `/memory/delete` deletes through `PanelState.delete_note`. Both memory writes go through `claire.memory.pg`. A refused request writes nothing and calls no model.

## Extension points

- **`PanelState` (`src/claire/panel/server.py:1444`) is the seam.** Every route takes one; subclass it to supply different sources, and every route works unchanged.
- **`RealPanelState.__init__` (`:1674`)** takes injectable `config_path`, `key_store`, `router_path`, `schedule_path`, `action_log_path`, `diary_path`, `vault_dir`, `transcript`, `chat_call`, `environ`, `now` and `self_check_factory`, so a test never touches the owner's real files. The memory methods are `memory_notes` (`:1522`), `memory_note`, `memory_search`, `save_note` (`:1545`) and `delete_note` (`:1556`).
- **`dispatch` is socket-free**, which is how the route surface is tested without a server; `create_handler` (`:4834`) binds one state to a handler class, and `create_server` (`:4846`) builds without starting.
- **`GET_VIEWS` (`:4412`)** is the read-route table. A new read route that takes no arguments is one entry; one that takes query arguments is dispatched by name, as `/memory` is.
- **The memory store owns its own format.** `claire.memory.pg` owns the rows and their frontmatter, and `claire.memory.notes` provides the shared name validation; the panel only calls them, so a change to the stored format is not a panel change.

## Failure modes

- **Port already taken.** `create_server` (`src/claire/panel/server.py:4846`) constructs a `PanelServer` (`:4839`) bound to the port; a taken port raises the bind `OSError`, and neither `create_server` nor `serve` swallows it, so `python -m claire.panel.server` exits with the OS error. `start.sh:554` checks the port first and declines to start a second panel.
- **Non-loopback bind without opt-in.** `PanelBindError` (`:499`), refused by name.
- **Malformed target, traversal, backslash, NUL.** `400 traversal-refused`.
- **Unknown path.** `404 route-not-found` refusal page. **Wrong method.** `405 method-not-allowed`, naming the allowed verbs in its fix; `GET /chat` is 405 by design.
- **Body too large, chunked, or an unparsable `Content-Length`.** `413 body-too-large`, or nothing is read and the connection is closed.
- **Credential-shaped field on a write.** `403 origin-refused`; nothing is written.
- **Missing or cross-origin `Origin`.** `403 origin-refused`.
- **Empty or over-long chat message.** `400`, naming the limit (`MAX_CHAT_CHARS`, `:290`, 4000).
- **Model gives no reply.** `502 llm-unavailable`, naming provider, model and base URL, and the transcript records a "no reply" note — an empty box is never rendered as an answer.
- **Note already exists / note missing / unsafe filename.** `_memory_notes_error` (`:4527`) turns the notes layer's own error into a `400` with a fix, and creates nothing.
- **Missing router document, config or diary.** Each section reports the absence by name with a fix and creates nothing; a section that cannot be built raises a named `PanelStateError` (`:516`).
- **Daemon or listener absent.** The panel still serves; it shows only the turns it holds, the self-check reports the component's failure, and chat fails as a `502` if the LLM backend is unreachable. There is no fallback that pretends the daemon answered.

## Tests

- `tests/test_panel_server.py` (1312 lines) — routes, guards, escaping, mask-only key rendering, the router budget, self-check and action sections.
- `tests/test_panel_chat.py` (872 lines) — the chat endpoint and the in-memory transcript, including the deliberate tripwire that pins the scrubber's limits.
- `tests/test_panel_over_a_socket.py` (529 lines) — the real HTTP stack, including the bodyless-GET and keep-alive/request-smuggling cases that in-process dispatch cannot see, plus the `/memory` routes over a socket.
- `tests/test_panel_memory.py` (564 lines) — the memories browser: listing, search, opening a memory, escaping, refusal of a traversal name and of malformed frontmatter, and the create/edit/delete writes through the memory store.
- `tests/test_panel_face.py` (305 lines) covers the face end to end: `/face/state` follows the state file and reads totally, the five states render distinctly, and the index server-renders the current state alongside its one poll script.

`panel.spec.md:9-11` names exactly these five.

## Documentation drift

The `/memory` surface, the memory seams and the `vault_dir` injectable are named in
`server.py` and in `panel.spec.md`, and `tests/test_panel_memory.py` covers them. So the
three things this spoke would otherwise have to flag are consistent in the tree as it
stands:

- the route table names `/memory`, `/memory/save` and `/memory/delete`
  (the spec's API table) and matches `GET_ROUTES`
  (`src/claire/panel/server.py:209`) and `POST_ROUTES` (`:220`);
- the page-section table carries the memories card and the browser itself (the
  spec's page and memory-editor sections);
- the seam list names `vault_dir` and the memory methods (the spec's Python
  surface, matching `PanelState` at `server.py:1444` and `RealPanelState` at
  `:1656`), and the spec writes memories through the same `claire.memory.pg` seam
  the panel uses.

Two things worth knowing rather than fixing:

- **The port 8765 claim is correct here.** `panel.spec.md:12`, `server.py:162`
  (`DEFAULT_PORT = 8765`), the README and `start.sh` agree. The conflicting
  `DEFAULT_PORT == 8765` line lives in the *phone bridge* spec, not this one; see
  [the phone bridge spoke](phone-bridge.md).
- **The panel is still not reachable from the daemon.** There is no IPC, socket or
  shared-memory channel: `RealPanelState` re-reads the files, the PostgreSQL memory
  store and the SQLite database the daemon writes. A change in one process is visible
  to the other only at the next read. That is a design boundary, not drift, but it is the first thing to
  check when a panel value looks stale.
