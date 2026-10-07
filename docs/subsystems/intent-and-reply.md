# Intent and the reply loop

**Authoritative specs:** [`src/claire/reply/reply.spec.md`](../../src/claire/reply/reply.spec.md),
[`planner.spec.md`](../../src/claire/reply/planner.spec.md),
[`prompts.spec.md`](../../src/claire/reply/prompts/prompts.spec.md),
[`tools/selection.spec.md`](../../src/claire/tools/selection.spec.md),
[`llm/llm.spec.md`](../../src/claire/llm/llm.spec.md).
Read this when you are changing what the model is asked, which tools it can see,
how many turns it gets, or what happens when the model misbehaves.

## Responsibility

Two decisions and one loop.

The **intent decision** lives in the listening subsystem: an LLM intent judge
decides whether an utterance was directed at Claire, given the transcript buffer
and the hot-window state. It is documented in the
[listening spoke](listening.md) because it is gated by capture state; the module
is `src/claire/listening/intent_judge.py` (`IntentJudge` at `:151`, `judge` at
`:397`, factory `create_intent_judge` at `:550`, `IntentJudgment` at `:122`).

The **reply loop** lives in `src/claire/reply/engine.py` and owns everything
after dispatch: redaction, context budgeting, tool routing, planning, memory
recall, prompt assembly, the agentic turn loop, tool execution, result digests,
and the final text. It has no audio or Qt imports, so it is callable from the
voice path, the text-chat path and the panel.

## Entry point and flow

`run_reply_engine(db, cfg, tts, text, dialogue_memory, language=None, quiet=False)`
at `src/claire/reply/engine.py:780` returns the reply text or `None`. The voice
path calls it with `tts=None` because the listener speaks the returned text
itself (`src/claire/listening/listener.py:1503`, then `:1547`); the text-chat
path passes `quiet=True` so replies do not land in the daemon's stdout.

Verified order inside `run_reply_engine`:

| Step | Where | Notes |
|---|---|---|
| Redact the query | `engine.py:808` (`utils/redact.py:36` `redact`) | Every reply turn is redacted before it reaches a prompt or a log. |
| Recent dialogue + context budget | `engine.py:843`–`:850`, `memory/conversation.py:684`–`:685` | Budget defaults are `DEFAULT_CONTEXT_MAX_TOKENS = 8000` and `DEFAULT_CONTEXT_MAX_MESSAGES = 40`; the budget is configured per turn at `engine.py:835`. Dialogue memory keeps messages by time, so the token/message ceiling is what stops a busy conversation shipping an unbounded prompt. |
| Tool routing | `engine.py:964` -> `tools/selection.py:385` `select_tools` | Strategy from `tool_selection_strategy` (default `"llm"`), with keyword (`selection.py:126`), embedding (`:163`) and LLM (`:240`) implementations and an always-include list (`:103`). The routed set is cached per query (`engine.py:957`/`:996`). |
| Planning | `engine.py:1080` -> `reply/planner.py:424` `plan_query` | The planner produces an ordered action plan; `plan_query` fails **open** on timeout (`planner_timeout_sec`, default `3.0`), because it is on the critical path. Plan helpers: `_parse_plan` (`:264`), `is_search_memory_step` (`:313`), `plan_requires_memory` (`:336`), `tool_steps_of` (`:350`), `plan_has_unresolved_tool_steps` (`:405`), `format_plan_block` (`:513`), `progress_nudge` (`:528`), `resolve_next_tool_call` (`:722`). |
| Memory recall | `engine.py:1238` -> `enrichment.py:27` `extract_search_params_for_memory`; `engine.py:1375` -> `enrichment.py:346` `digest_memory_for_query` | Which stores are searched is `memory_enrichment_source` (default `"all"`: diary + graph). The extractor result is cached per query (`engine.py:1231`/`:1245`). |
| Compound query split | `engine.py:1489` -> `reply/compound_query.py` `split_compound_query` | A multi-part question is decomposed so each part can drive a tool step. |
| System prompt | `engine.py:1514` `_build_initial_system_message`; `system_prompt.py:53` template; `reply/prompts/system.py` (`ASR_NOTE`, `INFERENCE_GUIDANCE`); `reply/prompts/model_variants.py` | One prompt template, varied by model size. The memory router block is loaded from the vault (`system_prompt.py:165` `resolve_default_router_path`, `:172` `load_router_from_vault`) under a byte budget (`:135`, default 6000). |
| Agentic loop | `engine.py:1850` `max_turns = cfg.agentic_max_turns`; `while turn < max_turns:` at `engine.py:1870` | Default `agentic_max_turns` is `8`. Each iteration sends the messages array through `chat_with_messages` (`engine.py:25`) and either calls tools or produces the final text. |
| Tool execution | `engine.py:1968` and `:2256` -> `tools/registry.py:315` `run_tool_with_retries` | Built-ins are dispatched by name; an external MCP tool is named `server__tool` and routed to `MCPClient.invoke_tool` (`registry.py:339`). See the [MCP spoke](mcp.md). |
| Result digest | `enrichment.py:613` `digest_tool_result_for_query`; `engine.py:2518` -> `enrichment.py:853` `digest_loop_for_max_turns` | One LLM call compresses tool output; `llm_digest_timeout_sec` (default `8.0`) bounds it. A failure returns the raw text rather than losing the result. |
| Tool carry-over | `engine.py:843`–`:846` (`tool_carryover_max_turns`, default `2`), `:1641` `_maybe_record_tool_carryover` | A follow-up that depends on the previous turn's tool result can reuse it for a bounded number of turns. |
| Memory update | `engine.py:2556`/`:2558` and `:2590`/`:2598` (`add_message`), `record_tool_turn` at `:1653` | The user and assistant turns are written to dialogue memory; diary and graph enrichment is asynchronous and owned by the memory subsystem. |

## Model output that is not a clean tool call

The engine spends real code on models that emit tool calls as text. If you are
touching the loop, these are the paths that keep a malformed turn from becoming
a spoken error:

- `_is_malformed_model_output` (`engine.py:223`) detects raw tool-protocol
  literals (`tool_calls:` as plain text).
- `_extract_text_tool_call` (`engine.py:296`) recovers `tool_calls: [...]`
  blocks, including JSON with dropped closing braces, and returns the call plus
  the prose around it.
- `_validate_tool_args_against_schema` (`engine.py:98`) and
  `_get_tool_input_schema` (`engine.py:83`) check arguments before execution.
- `_is_malformed_json_response` / `_extract_text_from_json_response`
  (`engine.py:1790`, `:1793`) handle a reply wrapped in JSON.
- `_text_tool_call_guidance` (`engine.py:174`) teaches the accepted format in the
  prompt for models that are not native tool callers.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `agentic_max_turns` | `8` | Iterations of the tool/reply loop. |
| `tool_carryover_max_turns` | `2` | How many extra turns may reuse a previous tool result. |
| `tool_selection_strategy` | `"llm"` | One of `all`, `keyword`, `embedding`, `llm`. |
| `tool_search_max_calls` | `3` | Ceiling on `tool_search` calls per turn. |
| `llm_chat_timeout_sec` | `45.0` | Final chat call. |
| `llm_tools_timeout_sec` | `15.0` | Tool-selection and tool-calling calls. |
| `llm_digest_timeout_sec` | `8.0` | Result digests. |
| `llm_embedding_timeout_sec` | `5.0` | Embeddings for recall and embedding-based selection. |
| `llm_profile_select_timeout_sec` | `30.0` | Profile/model selection calls. |
| `planner_timeout_sec` | `3.0` | Planner; fails open. |
| `memory_enrichment_source` | `"all"` | `all`, `diary` or `graph`. |
| `memory_digest_enabled` / `tool_result_digest_enabled` | `None` | `None` lets the engine choose by model size. |
| `llm_chat_model`, `llm_base_url`, `llm_api_key` | `""`, `""`, `""` | Empty values mean OmniRouter (`http://127.0.0.1:20129/v1`) and the `OMNIROUTE_API_KEY` environment variable; see the [configuration spoke](configuration.md). |

## Extension points

- **A new tool**: add it under `src/claire/tools/builtin/` (or register an MCP
  server) and it appears through `generate_tools_json_schema`
  (`tools/registry.py:179`) and `generate_tools_description` (`:238`); routing
  needs no change.
- **A new plan step**: planner step vocabulary is parsed by
  `_parse_plan_step_concrete` (`planner.py:616`) and validated with
  `_has_required_arguments` (`:710`).
- **A new model size variant**: `reply/prompts/model_variants.py`.
- **Prompt inspection**: `reply/prompt_dump.py` writes the assembled prompt for
  debugging without changing the loop.

## Failure modes

- **Provider unreachable or key missing**: the call site raises through
  `claire.llm` (`llm/factory.py`, `llm/errors.py`); the listener catches a reply
  exception, prints it, and speaks a short error (`listener.py:1507`–`:1514`)
  instead of staying silent.
- **Planner timeout**: `plan_query` returns no plan and the loop proceeds
  unplanned; the planner never blocks a reply.
- **Digest timeout**: the raw tool result is used.
- **Model emits text instead of a tool call**: recovered by the text-tool-call
  parser, or the turn ends with prose.
- **Turn budget exhausted**: the loop stops at `agentic_max_turns`; the digest
  path (`digest_loop_for_max_turns`) is what turns the mid-loop state into a
  final answer.
- **Concurrency**: the voice path holds `daemon.query_lock()` around
  `run_reply_engine` (`listener.py:1498`) so a voice query and a text-chat query
  cannot run against the same dialogue memory at once. Voice blocks rather than
  being dropped, then re-checks that its capture generation is still current
  (`listener.py:1499`).

## Tests

`tests/test_engine_planner_integration.py`, `test_planner.py` and the
`test_planner_*.py` family, `test_text_tool_call_parser.py`,
`test_tool_router_resolution.py`, `test_engine_tool_carryover.py`,
`test_engine_tool_carryover_guard.py`, `test_engine_tool_search_loop.py`,
`test_context_budget.py`, `test_prompts.py`, `test_system_prompt.py`,
`test_prompt_dump.py`, `test_compound_query.py`, `test_enrichment.py`,
`test_memory_injected_per_request.py`, `test_tool_digest_generation_budget.py`,
`test_greeting_no_tools.py`, `test_factory_dispatch_wiring.py`.

## Documentation drift

- **`reply.spec.md` and `llm.spec.md` describe the LLM layer as Ollama-defaulted.**
  `reply.spec.md:9` lists "the pluggable backend abstraction (`LLMBackend` ABC +
  `OllamaBackend` impl, factory at `get_llm_backend(settings)`)", and
  `llm/llm.spec.md:67` gives the default as `llm_provider` = `"ollama"` while
  `llm.spec.md:8` says the defaults "remain `127.0.0.1:11434`". The shipped
  default is `"llm_provider": "openai_compatible"` (`src/claire/config.py:561`)
  pointing at OmniRouter (`config.py:47`), with the key read from
  `OMNIROUTE_API_KEY` (`config.py:48`, `:567`). Ollama remains a supported
  provider, and `_resolve_provider` still falls back to it for an unknown value
  (`src/claire/llm/factory.py:28`–`:33`), so the spec's *fallback* claim is
  right and only its *default* is stale.
- **`reply.spec.md` calls `src/claire/system_prompt.py` the single unified
  `SYSTEM_PROMPT`** (`reply.spec.md:8`). The module still exists
  (`src/claire/system_prompt.py:53` holds the template) but the prompt is
  assembled with per-size variants from `src/claire/reply/prompts/system.py` and
  `model_variants.py`.
- **Two ceiling figures exist for the tool-routing and embedding calls.** The
  config defaults are `llm_tools_timeout_sec = 15.0` and
  `llm_embedding_timeout_sec = 5.0` (`src/claire/config.py`), while the reply
  engine's `getattr` fallbacks are `8.0` for the tool router
  (`src/claire/reply/engine.py:971`, `:1240`) and `10.0` for the extractor and
  embedding probe (`engine.py:974`, `:1272`). Because `Settings` always carries
  the attributes, the config figures are what run and the fallbacks are only
  reachable with a hand-built settings object. `tests/test_reply_path_timeouts.py`
  pins the fail-fast property of the whole reply path; if you change one of these
  ceilings, change the matching fallback too.
- **`llm.spec.md`'s public surface is narrower than the package.** It documents
  `OllamaBackend` and `OpenAICompatibleBackend` as the implementations; the
  package also carries `llm/tiers.py` and `llm/errors.py` and the OpenAI-compatible
  backend is the one the default route uses. Treat the spec's class list as
  illustrative, and read `llm/factory.py` for the real dispatch.

## Related spokes

- [Listening and speech capture](listening.md) — the intent judge and dispatch.
- [Long-term memory](memory.md) — the store behind recall and digests.
- [MCP](mcp.md) — the external tools the loop can call.
- [Configuration](configuration.md) — the defaults above.
