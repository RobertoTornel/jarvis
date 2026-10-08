# `claire/llm/gemini.py` — Gemini embeddings

## Responsibility

Own Claire's only Google Gemini HTTP call: embeddings. The module implements
`GeminiEmbeddingBackend`, an `LLMBackend` used exclusively through
`get_embedding_backend(settings)` when `embedding_provider` is `"gemini"` (or
its `"google"` alias). It is **not** a chat backend: `get_llm_backend` never
returns it, and `direct` / `streaming` / `chat` raise `NotImplementedError`.

## Interface

| Name | Kind | Meaning |
|---|---|---|
| `GEMINI_PROVIDER` | constant | `"gemini"` — the config value |
| `GEMINI_BASE_URL` | constant | `https://generativelanguage.googleapis.com/v1beta` |
| `GEMINI_DEFAULT_EMBED_MODEL` | constant | `gemini-embedding-2` |
| `GEMINI_TEXT_ONLY_EMBED_MODEL` | constant | `gemini-embedding-001` |
| `GEMINI_OUTPUT_DIMENSION` | constant | `EMBEDDING_DIMENSION` (768) |
| `uses_task_prefixes(model)` | function | whether the model is Embedding 2 (prefix instead of `taskType`) |
| `format_for_task(text, model, task_type)` | function | applies the documented Embedding 2 task prefix |
| `GeminiEmbeddingBackend.embed(text, model, timeout_sec, task_type)` | method | one text via `:embedContent` |
| `GeminiEmbeddingBackend.embed_batch(texts, model, timeout_sec, task_type)` | method | many texts via `:batchEmbedContents`, one result per input in order |

## Wire contract

- `POST {base_url}/models/{model}:embedContent`
  body `{"model": "models/{model}", "content": {"parts": [{"text": ...}]},
  "outputDimensionality": 768, "taskType": <native models only>}`.
- `POST {base_url}/models/{model}:batchEmbedContents`
  body `{"requests": [<the per-text body above>, ...]}`, chunked at 100.
- Auth is the header `x-goog-api-key`. The `?key=` query-string form is
  deliberately **not** used: it would put the secret in every URL, log line and
  proxy trace. Exception messages are never logged verbatim for the same
  reason — only the exception class.
- No key configured ⇒ no request is made; the call returns `None` and logs why.

## Invariants

1. **Dimension.** Every returned vector is exactly `EMBEDDING_DIMENSION` (768)
   floats. Anything else is discarded and logged; it is never handed to a
   caller that might store it. The index is not migratable, so this is a hard
   contract — see `claire/utils/embedding_vector.spec.md`.
2. **Task asymmetry.** `embed` / `embed_batch` accept the interface's
   `task_type`. On Embedding 1 models this becomes the native `taskType` field;
   on Embedding 2 models it becomes the documented text prefix. Unknown values
   are dropped rather than sent, so a typo cannot become a 400.
3. **Batch alignment.** `embed_batch` returns exactly one entry per input, in
   order. A response whose length does not match the request is discarded
   wholesale rather than shifted onto the wrong memories.
4. **Failure is soft and loud.** Every failure — HTTP error, network error,
   missing key, malformed or wrong-length response — returns `None` (or a list
   of `None`s) after a `debug_log` line, so callers fall back to keyword search
   and the operator can see why.

## Model choice

`gemini-embedding-2` is the default because the owner asked for the newest
embedding model. `gemini-embedding-001` is the stable text-only alternative and
is selected purely by config. Google documents the two models' embedding spaces
as **incompatible**, so changing `embedding_model` between them is a re-index,
not a config flip; that is documented for users in `docs/CONFIGURATION.md`.

## API surface choice

The native `generativelanguage` API is used rather than the OpenAI-compatible
`/v1beta/openai/embeddings` surface, because (a) `taskType` has no slot in the
OpenAI schema and is the reason retrieval quality holds up, (b)
`outputDimensionality` is likewise native-only, and (c) `batchEmbedContents`
batches properly. Reusing `OpenAICompatibleBackend` would have been less code
and strictly worse embeddings.

## Tests

`tests/test_gemini_embeddings.py` (unit, stubbed `requests.post`): endpoint and
model id, `x-goog-api-key` header shape, `outputDimensionality == 768`, both
task-type paths, batching and chunking, positional degradation, dimension
refusal, failure paths, factory/config wiring, and the keyword fallback.
No test makes a live network call.

## Not covered here

Chat, tool calling and model listing (the module refuses them);
`src/claire/llm/llm.spec.md` covers the shared backend interface, which another
change owns.
