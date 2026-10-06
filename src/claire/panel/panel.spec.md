# Claire Web Panel (R-WEB-1)

A small, **loopback-only** web page so the owner can see — and change — the things that are
otherwise buried in config. One page, server-rendered, standard library only.

## Scope

- Files: `src/claire/panel/__init__.py`, `src/claire/panel/server.py`.
- Tests: `tests/test_panel_server.py`.
- Entry point: `python -m claire.panel.server` → `http://127.0.0.1:8765/`.
- Owns: rendering one HTML page and a tiny set of read routes from **injected state**, the
  provider/model selector form and its write endpoint, and the HTTP guards around them.
- Does **not** own: any of the state it shows. `claire.config` owns provider/model resolution,
  `claire.platform.keys` owns key storage and masking, `claire.memory.router` owns `CLAIRE.md`
  and its budget framing, `claire.schedule` owns the calendar, `claire.platform.service` owns
  the self-check. The panel reads them and never edits them.
- No new packages. `http.server`, `html`, `json`, `re`, `dataclasses`, `urllib.parse` only — no
  Flask/FastAPI, no JavaScript, no build step, no template engine.
- No `__main__.py` in this package (write scope): `python -m claire.panel.server` is the
  documented invocation, and it works because `server.py` uses absolute imports.

## The page

| Section | Source | Shows |
| --- | --- | --- |
| Provider and model | `config.json` via `RealPanelState.provider_model()` | selected provider, resolved model, endpoint, plus the selector form |
| Providers and keys | `claire.platform.keys` | one row per known credential: **masked** value (last-4 + fingerprint) or `absent`, plus its source |
| Memory router | `claire.memory.router` | `CLAIRE.md` path, `size_bytes` of `budget_bytes` (default 6000), over/under verdict, and the **verbatim** over-budget warning the prompt uses |
| Today's calendar | `claire.schedule` (`load_schedule` + `list_events`) | today's occurrences, formatted through the calendar's own `format_spoken` |
| Self-check | `claire.platform.service.SelfCheck` | summary line, one-click link to the full text report, and every non-READY check with its fix |
| Last actions | action journal (JSONL) | the last 8 records: when, action, outcome, detail |

Routes (all exact strings; `GET_ROUTES` is the complete list and tests iterate it):

| Route | Method | Response |
| --- | --- | --- |
| `/` | GET | `200` HTML — the page |
| `/self-check` | GET | `200` `text/plain` — the self-check report |
| `/healthz` | GET | `200` `application/json` — `{panel, ok, summary, generated_at}` |
| `/favicon.ico` | GET | `204` |
| `/select` | POST | `303` to `/?changed=1` on success; a refusal page otherwise |

The body of every rendered page is HTML. Content that came from the internet (calendar titles
and notes, action details, model ids, error text, the router body) is **escaped**: every
interpolated value passes through `_esc()`, which runs `claire.utils.redact.scrub_secrets()` and
then `html.escape(..., quote=True)`. HTML in a data value is data, never markup.

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
   - **The page has no key field**, and `POST /select` refuses any body carrying one (see 3).
     Keys are set out-of-band: the key store, or the provider's environment variable
     (`OMNIROUTE_API_KEY`). The page says so in as many words.
   - **No secret is logged.** `PanelHandler.log_message` is overridden so the raw request line
     (which can carry a credential in its query string) is never written to stderr: the panel
     logs the method and the *path* only, scrubbed and truncated. The panel never logs a form
     body or a query value.
2. **Loopback only, by default and by guard.** `DEFAULT_BIND_HOST` is `127.0.0.1`.
   `is_loopback_host()` accepts `127.0.0.0/8`, `::1` and `localhost` — and **not** the empty
   string, because `--host ""` and an `Origin` with no host both mean "somewhere else".
   `guard_bind_host()` raises `PanelBindError` for anything else unless `allow_remote=True`, and
   then logs a loud `WARNING`: the panel is **plain HTTP — no TLS, no authentication**, readable
   by anyone who can reach the interface. `create_server()` runs the guard *before* it binds, so
   a refused host never opens a socket.
3. **The write endpoint is forgery-resistant.** `check_write_origin()` runs before the body is
   looked at and refuses with `PanelOriginError` (`403 origin-refused`) unless:
   - `Origin` is present (silence is not a same-origin proof),
   - its scheme is `http`,
   - its host is loopback,
   - its host equals the `Host` header's host,
   - any port on either header equals the port the panel is running on,
   - `Sec-Fetch-Site`, when sent, is `same-origin` or `none`.
   The body is then parsed as `application/x-www-form-urlencoded`, capped at
   `MAX_BODY_BYTES` (64 KiB, `413 body-too-large`), and refused (`403 origin-refused`) if it
   carries any name in `FORBIDDEN_KEY_FIELDS`. Provider and model are validated against
   `PanelState.available_providers()` and a conservative model-id pattern
   (`^[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,127}$`) before `select_model()` is called; a refused
   request writes nothing.
4. **No directory traversal, because no path is served.** Every route is an exact string in
   `GET_VIEWS`/the dispatch table. `parse_target()` percent-decodes the request target and
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
| `PanelState` | The injectable source: `provider_model`, `key_status`, `config_credentials`, `router_status`, `calendar_today`, `self_check`, `last_actions`, `available_providers`, `available_models`, `select_model`. The base class implements none of the sections (they raise `PanelStateError`); `PROVIDERS` and `config_credentials()` have defaults. |
| `RealPanelState` | The real sources. Every path is injectable (`config_path`, `key_store`, `router_path`, `schedule_path`, `action_log_path`, `environ`, `now`, `self_check_factory`). Reads `CLAIRE.md` with `create=False` — the panel never creates a memory file behind the owner's back. Caches the self-check for `SELF_CHECK_TTL_SECONDS` (30). |
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
