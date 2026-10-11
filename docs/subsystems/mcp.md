# MCP integration

**Authoritative spec:** [`src/claire/tools/external/mcp_runtime.spec.md`](../../src/claire/tools/external/mcp_runtime.spec.md).
Read this when you are changing how external MCP servers are configured, spawned,
kept alive, discovered, dispatched, or shut down, or when a configured server's
tools do not reach the reply loop.

Specs next to the code win over this document. Where this page and a spec
disagree, the spec and the code decide; contradictions found while writing this
page are listed under [Documentation drift](#documentation-drift).

The user-facing counterpart — worked configurations, the server template, and a
troubleshooting table — is [`docs/MCP_SERVERS.md`](../MCP_SERVERS.md).

## Responsibility

The MCP subsystem lets Claire drive tools that live in external programs that
speak the Model Context Protocol. It owns:

- reading the `mcps` configuration section and resolving each server's launch
  command;
- spawning each server as a stdio subprocess and completing the MCP handshake;
- keeping one session per server open for the daemon's lifetime, so stateful
  servers (for example browser automation that owns a Chrome process) survive
  between calls;
- discovering each server's tool list and namespacing the tools as
  `<server>__<tool>`;
- dispatching a model-emitted `<server>__<tool>` name to `call_tool` and
  flattening the result back into a tool reply;
- tearing every worker and the runtime thread down at daemon shutdown.

It does **not** own the reply loop, tool routing, or the built-in tool registry.
The reply engine decides which tools a turn may use and calls the dispatcher;
`registry.py` is the seam between the two.

**Transport is stdio only.** There is no HTTP or SSE client path: a server whose
config sets `transport` to anything but `"stdio"` is rejected with
`NotImplementedError` (`mcp_client.py:198`–`:200`, `_require_stdio_cfg` at
`mcp_client.py:288`–`:300`). HTTP/SSE servers must be fronted by a stdio proxy
(a `command` that itself speaks to the remote endpoint).

## Key modules

| Module | Owns |
|---|---|
| [`src/claire/tools/external/mcp_client.py`](../../src/claire/tools/external/mcp_client.py) | Config → command resolution, `stdio_client` transport construction, the public sync API (`MCPClient.list_tools` at `:235`, `MCPClient.invoke_tool` at `:263`), the async one-shot path (`MCPClient._session` at `:194`), the response flattening (`_result_to_dict` at `:303`), and the public session-loss type `MCPServerSessionError` at `:18`. |
| [`src/claire/tools/external/mcp_runtime.py`](../../src/claire/tools/external/mcp_runtime.py) | The process-wide singleton runtime (`get_runtime` at `:99`, `shutdown_runtime` at `:108`), one background asyncio loop on the thread named `ClaireMCPRuntime` (`:154`–`:157`), one `_ServerWorker` per server (`:321`), per-server serialisation, idle reaping, invoke timeouts, and the retry-once-on-death policy. |
| [`src/claire/tools/registry.py`](../../src/claire/tools/registry.py) | Discovery at startup (`initialize_mcp_tools` at `:56`, `discover_mcp_tools` at `:126`), the in-memory tools cache, namespacing as `<server>__<tool>` at `:149`, schema/description generation for the model (`generate_tools_json_schema` at `:177`, `generate_tools_description` at `:236`), and dispatch (`run_tool_with_retries` at `:313`, the MCP branch at `:329`–`:344`). |
| [`src/claire/reply/engine.py`](../../src/claire/reply/engine.py) | Loads the MCP catalog into the turn (`:886`–`:893`), refreshes it on a new conversation (`:876`–`:883`), feeds it to the tool router (`select_tools` at `:1017`), and to the planner schema (`:1083`). |
| [`src/claire/daemon.py`](../../src/claire/daemon.py) | Startup discovery before the voice loop (`:1451`–`:1481`) and runtime teardown (`shutdown_runtime` on the smoke-test path at `:1627`–`:1628` and in the main `finally` at `:1787`–`:1788`). |

## Data and control flow

1. **Config.** `load_settings()` reads `config.json` and normalises the `mcps`
   value to a dict (`config.py:1117`, `mcps = _ensure_dict(...)`). The default is
   empty (`config.py:788`, `"mcps": {}`). `Settings.mcps` is declared at
   `config.py:343`.
2. **Startup discovery.** `daemon.main` reads `cfg.mcps` and, when non-empty,
   calls `initialize_mcp_tools(mcps, verbose=False)` (`daemon.py:1456`,
   `initialize_mcp_tools` imported at `daemon.py:45`). This caches the server
   config globally and calls `discover_mcp_tools` (`registry.py:71`).
   `discover_mcp_tools` constructs one `MCPClient(mcps_config)`
   (`registry.py:136`) and calls `client.list_tools(server_name)` once per
   configured server (`registry.py:142`). A server that raises is caught per
   server, logged, and recorded in the returned `errors` map; the loop continues
   with the next server (`registry.py:160`–`:168`). A failure of the whole call
   returns `{}` plus a `"_global"` error (`registry.py:172`–`:174`). Discovery
   therefore never aborts daemon startup.
3. **Session spawn.** `MCPClient.list_tools` validates the config and imports
   the runtime lazily (`mcp_client.py:242`–`:245`), then calls
   `runtime.list_tools(server_name, cfg)` (`mcp_client.py:247`). On first use,
   `get_runtime()` creates the `_PersistentMCPRuntime`, which starts the
   `ClaireMCPRuntime` background thread and its asyncio loop on the first
   `get_runtime()` call (`mcp_runtime.py:99`–`:105`, `:132`–`:157`).
   `_get_worker` returns a cached worker when it is alive and its config still
   equals the requested one (`mcp_runtime.py:245`); otherwise it shuts the old
   worker down and creates a fresh `_ServerWorker` and calls `worker.start()`
   (`mcp_runtime.py:247`–`:263`).
4. **Handshake.** `_ServerWorker.start` schedules `_run` on the loop and then
   blocks on `_ready.result(timeout=_SETUP_TIMEOUT_SEC)`
   (`mcp_runtime.py:356`–`:365`). `_run` builds the transport with
   `client._connect_stdio(self.config)` (`mcp_runtime.py:370`), opens the
   `ClientSession`, awaits `session.initialize()` and only then sets `_ready`
   (`mcp_runtime.py:375`–`:379`). Discovery therefore returns only after the
   server is genuinely connected.
5. **Transport construction.** `_connect_stdio` resolves `command` to an
   absolute path (`_resolve_command` at `mcp_client.py:50`, which probes `PATH`,
   then fixed directories at `:30`–`:35`, then version-managed globs at
   `:39`–`:42`, then the user's login shell), expands `~` in `args`
   (`mcp_client.py:162`–`:163`), merges `env` over the daemon's environment and
   prepends the resolved command's directory to `PATH` so `#!/usr/bin/env node`
   shebangs work (`mcp_client.py:164`–`:176`), builds
   `StdioServerParameters(command=command, args=args, env=env)`
   (`mcp_client.py:177`), and wraps `stdio_client(params, errlog=devnull)` so the
   `/dev/null` handle used to silence the server's stderr is closed on every exit
   path (`_StdioConnection` at `mcp_client.py:109`–`:137`, construction at
   `:181`–`:191`).
6. **Tool catalog.** Each discovered tool becomes a `ToolSpec` keyed
   `f"{server_name}__{tool_name}"` with the server's `description` and
   `inputSchema` (`registry.py:149`–`:158`). The reply engine loads the cached
   catalog for the turn (`engine.py:886`–`:893`) and hands it to the tool router
   (`engine.py:1017`–`:1029`, `select_tools(..., mcp_tools=mcp_tools, ...)`) and to the
   planner schema (`engine.py:1083`). The router's hot-window cache key includes
   the sorted MCP tool names, so a mid-window catalogue change invalidates the
   cached routing decision (`engine.py:1007`).
7. **Call.** When the model emits a name containing `__`, `run_tool_with_retries`
   splits it on the first `__` (`registry.py:329`–`:330`), checks the server is
   configured (`registry.py:332`), constructs an `MCPClient`
   (`registry.py:337`) and calls `client.invoke_tool(...)` (`registry.py:338`).
   `invoke_tool` routes to `runtime.invoke(...)` (`mcp_client.py:278`–`:283`).
   The runtime submits the call to the worker's queue with a single
   `call_soon_threadsafe(queue.put_nowait, ...)` hop and blocks on a
   `concurrent.futures.Future` (`mcp_runtime.py:505`–`:509`); the worker awaits
   `session.call_tool(tool_name, arguments or {})` and resolves the future
   (`mcp_runtime.py:412`–`:424`). Calls to one server are serialised by its
   queue; different servers have independent workers and run in parallel
   (`mcp_runtime.py:16`–`:23`).
8. **Result.** `_result_to_dict` flattens the MCP response's `content` into the
   internal dict (`mcp_client.py:303`). The dispatcher reads `isError` and the
   flattened text and returns a `ToolExecutionResult`
   (`registry.py:339`–`:341`). Any exception on the call path — including
   `MCPServerSessionError` — becomes a failed `ToolExecutionResult` with the
   error text (`registry.py:342`–`:344`).
9. **Teardown.** The runtime is a singleton for the daemon's lifetime. The daemon
   calls `shutdown_runtime()` on both exit paths (`daemon.py:1627`–`:1628` for
   the smoke-test path, `daemon.py:1787`–`:1788` in the main `finally`).
   `shutdown_runtime()` clears the singleton and calls `instance.shutdown()`
   (`mcp_runtime.py:108`–`:118`). `_PersistentMCPRuntime.shutdown` marks itself
   closed, shuts every worker down, stops the loop with
   `loop.call_soon_threadsafe(loop.stop)` and joins the thread with a
   `_SHUTDOWN_THREAD_JOIN_SEC` (5 s) budget (`mcp_runtime.py:283`–`:311`).
   `_ServerWorker.shutdown` asks the task to exit and cancels it if it stalls
   (`mcp_runtime.py:524`–`:553`).

## Configuration keys

The `mcps` key is a dict of server name → server dict. It has no defaults
(`config.py:788`) and is normalised with `_ensure_dict` (`config.py:1117`), so a
non-dict value becomes `{}`. Per-server keys, from `mcp_client.py` and
`mcp_runtime.py`:

| Key | Type | Default | Effect |
|---|---|---|---|
| `command` | str | required | Executable to launch. Resolved to an absolute path (`mcp_client.py:50`); a non-existent absolute path raises `FileNotFoundError` (`mcp_client.py:61`–`:64`). `~` is **not** expanded in `command`. |
| `args` | list | `[]` | Argument list; a leading `~` in string arguments is expanded (`mcp_client.py:162`–`:163`). |
| `env` | dict | `{}` | Extra environment variables merged **over** the daemon's own environment (`mcp_client.py:164`, `:172`–`:174`). These are the only per-server secret channel; there is no keychain indirection for them. |
| `transport` | str | `"stdio"` | Only `"stdio"` is accepted; anything else raises `NotImplementedError` (`mcp_client.py:198`–`:200`, `:297`–`:299`). |
| `timeout_sec` | float \| null | `120.0` (`_DEFAULT_INVOKE_TIMEOUT_SEC`, `mcp_runtime.py:55`) | Bounds one `call_tool` and one `list_tools` round trip (`_resolve_invoke_timeout` at `mcp_runtime.py:59`). Non-finite, non-positive, boolean or non-numeric values fall back to the default with a debug log (`mcp_runtime.py:65`–`:92`). On expiry a bare `concurrent.futures.TimeoutError` propagates if the worker is still alive (`mcp_runtime.py:509`–`:522`). |
| `idle_timeout_sec` | float \| null | `null` | When set, the worker self-terminates after that long with an empty queue (`mcp_runtime.py:347`–`:354`, `:458`–`:469`). Unset keeps the subprocess resident for the daemon's lifetime. A boolean or non-numeric value is treated as unset (`mcp_runtime.py:348`–`:354`). |

Fixed internal timeouts, not configurable:

- `_SETUP_TIMEOUT_SEC = 30.0` — the handshake wait in `_ServerWorker.start`
  (`mcp_runtime.py:56`, `:365`).
- 5 s — the loop-setup scheduling wait and the loop-ready wait
  (`mcp_runtime.py:361`, `:158`).
- `_SHUTDOWN_THREAD_JOIN_SEC = 5.0` — the runtime thread join budget
  (`mcp_runtime.py:57`, `:306`).

Two config keys outside `mcps` affect MCP behaviour:

- `tool_selection_strategy` (default `"llm"`, `config.py:763`, validated at
  `config.py:1095`–`:1097`) decides whether all tools, including MCP tools, are
  offered to the model or only a routed subset.
- `voice_debug` (env `CLAIRE_VOICE_DEBUG`, `config.py:863`) gates the `debug_log`
  lines tagged `"mcp"` (see `debug.py:25`–`:35`).

## Extension points

- **Add an MCP server.** Add an entry under `mcps` in `config.json` with at least
  `command` (and `args`). No code change is needed: the daemon discovers it at
  the next start (`daemon.py:1451`–`:1481`). A running daemon does not pick up a
  newly added server until restart or an explicit `refreshMCPTools` call, because
  the catalog is cached in `registry.py` and refreshed only at startup and on a
  new conversation (`engine.py:876`–`:883`).
- **Refresh without restarting.** The built-in `refreshMCPTools` tool calls
  `refresh_mcp_tools()` and re-runs discovery against the cached config
  (`src/claire/tools/builtin/refresh_mcp_tools.py:13`–`:44`;
  `registry.py:85`–`:108`).
- **Change a server's settings.** Editing that server's config replaces only its
  worker, because `_get_worker` compares the cached config dict for equality and
  rebuilds on mismatch (`mcp_runtime.py:245`–`:263`).
- **Opt out of residency.** Set `idle_timeout_sec` on a stateless server.
- **Write a new server.** `templates/mcp_server/` is a runnable stdin/stdout
  server plus `config.example.json`; `tests/test_mcp_server_template.py` launches
  it through Claire's real client.
- **Add a transport.** The seam is `MCPClient._session` and
  `_require_stdio_cfg` in `mcp_client.py`; both currently reject non-stdio
  transports, and `_ServerWorker._run` is hardcoded to
  `client._connect_stdio(self.config)` (`mcp_runtime.py:370`), so a second
  transport needs changes in both files.

## Failure modes

| Symptom | What the code actually does |
|---|---|
| The `mcp` package is absent | It is a hard dependency (`requirements.txt:62`, `mcp==1.13.1`), and `mcp_client.py:9`–`:10` import it unconditionally, so `registry.py` fails to import. The `if MCPClient is None` guard at `registry.py:334` is therefore unreachable. |
| `command` not found | `_resolve_command` raises `FileNotFoundError` with a message naming the command before any process starts (`mcp_client.py:61`–`:64` for an absolute path, `:103`–`:106` when it is not on `PATH`). At discovery this is caught per server and reported by name (`registry.py:160`–`:168`); the daemon prints the error next to the server (`daemon.py:1471`–`:1472`). |
| Server starts but the handshake fails or hangs | `_run` surfaces the exception through `_ready` (`mcp_runtime.py:428`–`:438`), so `start()` raises it (`mcp_runtime.py:365`). A hang instead raises `concurrent.futures.TimeoutError` after 30 s. Either way the worker never enters the cache and the failure is reported as that server's discovery error. |
| Configured server has no tools | Discovery succeeds with zero tools; the daemon prints a "no tools discovered" line for it (`daemon.py:1473`–`:1474`). |
| Subprocess dies during a call | The worker's `finally` sets `alive = False` and drains the queue, resolving every outstanding future with `_WorkerDeadError` (`mcp_runtime.py:439`–`:456`). The runtime catches that, drops the worker, creates a fresh one and retries the call **once** (`mcp_runtime.py:187`–`:196` for `invoke`, `:223`–`:230` for `list_tools`). If the retry also fails, `MCPClient.invoke_tool` raises `MCPServerSessionError` (`mcp_client.py:284`–`:285`), which the dispatcher turns into a failed `ToolExecutionResult` (`registry.py:342`–`:344`). |
| Call exceeds `timeout_sec` while the worker is alive | A plain `concurrent.futures.TimeoutError` propagates to the caller; the worker is not killed (`mcp_runtime.py:509`–`:511`, `:519`–`:522`). |
| Tool returns an error payload | It is not an exception: `isError` on the flattened response becomes `success=False` with the text as `error_message` (`registry.py:339`–`:341`). |
| Idle timeout fires mid-call | If the worker exits while a call is queued, the drain resolves it as `_WorkerDeadError` and the normal retry-once path applies (`mcp_runtime.py:458`–`:469`, `:439`–`:456`). |
| Shutdown stalls | The runtime stops the loop after asking workers to exit and joins the thread with a 5 s budget, logging if the thread is still alive rather than hanging (`mcp_runtime.py:290`–`:311`). |

## Where its tests live

- [`tests/test_mcp_client.py`](../../tests/test_mcp_client.py) — the runtime
  contract: session reuse across calls (`:86`), retry on session loss (`:123`),
  worker replacement on config change (`:171`), startup-failure propagation
  (`:202`), per-server isolation (`:231`), persistent discovery (`:266`),
  timeout validation and per-server `timeout_sec` (`:306`, `:321`, `:376`,
  `:432`), and command resolution / `PATH` injection (`:481`, `:691`, `:792`).
- [`tests/test_mcp_discovery.py`](../../tests/test_mcp_discovery.py) — the
  `server__tool` convention and registry/engine integration.
- [`tests/test_mcp_integration.py`](../../tests/test_mcp_integration.py) — reply
  engine integration.
- [`tests/test_mcp_e2e.py`](../../tests/test_mcp_e2e.py) — config → discovery →
  registration pipeline.
- [`tests/test_config_mcps.py`](../../tests/test_config_mcps.py) — `mcps`
  defaults and normalisation.
- [`tests/test_mcp_server_template.py`](../../tests/test_mcp_server_template.py) —
  the shipped template round-trips through `MCPClient`.
- [`tests/test_mcp_catalogue.py`](../../tests/test_mcp_catalogue.py) — catalogue
  integrity and config entry mapping.

## Documentation drift

1. **Where MCP server secrets belong.**
   [`docs/MCP_SERVERS.md:271`–`:272`](../MCP_SERVERS.md#L271) tells the owner to
   "keep secrets in `config.json`, never in the server's source", and the worked
   examples in `docs/CONFIGURATION.md` put tokens inline in each server's `env`
   (`docs/CONFIGURATION.md:449`–`:453`, `:516`–`:520`, `:573`–`:583`). `AGENTS.md:5`
   says the API key "is never written into a repository file or a config file",
   and `AGENTS.md:8` says secrets live in the OS keychain or in 0600 files
   outside the repository. The code sides with the
   MCP docs for `mcps[*].env`: `mcp_client.py:164` reads `env` straight from the
   server config and passes it to the child at `:177`; there is no keychain
   indirection for MCP server `env`. Server `command`/`args` can name an external
   0600 env file, but nothing in the MCP path does that for you.
2. **No contradiction found between the MCP spec and the MCP code.** The
   `mcp_runtime.spec.md` tables (lifecycle, invariants, the `idle_timeout_sec` /
   `timeout_sec` defaults, the note that `invoke_tool_async` keeps its one-shot
   session) match `mcp_runtime.py` and `mcp_client.py` line for line as read for
   this page. Wording is not repeated here to avoid creating a third source of
   truth.
