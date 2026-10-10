# Configuration

**The single source of defaults is `get_default_config()` in [src/claire/config.py](../../src/claire/config.py#L552); the loader, the desktop Settings window, the generated example and the panel all read that function or a `Settings` object built from it, and there is no other defaults table in the tree.**

**Read this when** you need to know which file a setting is actually read from, why a value in `config.json` did or did not take effect, how to add or regenerate a config key, or what happens when the config file is missing, malformed or wrong-typed. This page is the reference for *how configuration works*; for the meaning of individual keys — model routes, speech, search, MCP, troubleshooting — read [docs/CONFIGURATION.md](../CONFIGURATION.md) and the key lists in the [README](../../README.md#configuration).

[← Back to Claire](../../README.md)

## 1. Where configuration lives

| What | Resolution order (first match wins) | Implemented at |
| :--- | :--- | :--- |
| Config file | `$CLAIRE_CONFIG_PATH` (tilde-expanded) → `$XDG_CONFIG_HOME/claire/config.json` → `~/.config/claire/config.json` | [config.py:342](../../src/claire/config.py#L342) (`default_config_path`), [config.py:849](../../src/claire/config.py#L849), [config.py:477](../../src/claire/config.py#L477) |
| Runtime secrets env file (`0600`) | `$CLAIRE_ENV_FILE` → `$XDG_CONFIG_HOME/claire/claire.env` → `~/.config/claire/claire.env` | [config.py:808](../../src/claire/config.py#L808) (`runtime_env_file`); mirrored at [service.py:404](../../src/claire/platform/service.py#L404) |
| Key store (`0600` JSON, never inside the repository) | `$CLAIRE_KEY_STORE` → `$XDG_CONFIG_HOME/claire/keys.json` → `~/.config/claire/keys.json` | [keys.py:278](../../src/claire/platform/keys.py#L278) (`default_store_path`) |
| Memory database | `db_path` config key; default `~/.local/share/claire/claire.db` | [config.py:67](../../src/claire/config.py#L67), [config.py:873](../../src/claire/config.py#L873) |
| Markdown memory vault | `vault_dir` config key; default `~/.local/share/claire/vault`, and `$CLAIRE_VAULT_DIR` overrides it for a run | [config.py:73](../../src/claire/config.py#L73), [config.py:877](../../src/claire/config.py#L877), [notes.py:133](../../src/claire/memory/notes.py#L133) |

`_default_db_path()` creates the data directory as a side effect of asking for the default; `_default_vault_dir()` deliberately does not, and returns the portable `~/...` form because that value is also what the generated example ships ([config.py:67](../../src/claire/config.py#L67), [config.py:73](../../src/claire/config.py#L73)).

### Precedence

The loader applies exactly these layers, in this order ([config.py:844](../../src/claire/config.py#L844)–[config.py:1140](../../src/claire/config.py#L1140)):

1. **Defaults** from `get_default_config()` ([config.py:552](../../src/claire/config.py#L552)).
2. **Migrations** of the on-disk file in memory, which rewrite older key shapes and stamp `_config_version` ([config.py:389](../../src/claire/config.py#L389)–[config.py:457](../../src/claire/config.py#L457)).
3. **The config file**, merged over the defaults with `{**defaults, **cfg_json}` — a key present in the file, including an explicit `null` or `""`, wins ([config.py:841](../../src/claire/config.py#L841)–[config.py:842](../../src/claire/config.py#L842)).
4. **The environment — but only for specific fields, not as a general layer.** On the settings path the loader consults four variables: `CLAIRE_CONFIG_PATH` for the file location ([config.py:825](../../src/claire/config.py#L825)), and then `CLAIRE_VOICE_DEBUG` ([config.py:846](../../src/claire/config.py#L846)), `OMNIROUTE_API_KEY` (a fallback only when `llm_api_key` resolves empty, [config.py:890](../../src/claire/config.py#L890)) and `CLAIRE_WHISPER_BACKEND` (which *outranks* the file's `whisper_backend`, [config.py:1007](../../src/claire/config.py#L1007)). Every other key can only come from the file.

There is no CLI layer: the `claire` parser accepts only `--smoke-test` and `--version`, so no command-line flag sets or overrides a config value ([cli.py:19](../../src/claire/cli.py#L19)–[cli.py:28](../../src/claire/cli.py#L28)).

The runtime env file is loaded before the config file, with `override=False` twice: a real process environment variable outranks `claire.env`, which in turn never overwrites a variable already set ([config.py:826](../../src/claire/config.py#L826), [config.py:838](../../src/claire/config.py#L838), [config.py:841](../../src/claire/config.py#L841)). `load_runtime_env()` also performs a bare `load_dotenv(override=False)`, which reads `./.env` from the current working directory; the file it is *documented* to read is `runtime_env_file()`.

### Who reads and who writes the file

- `load_settings()` is the daemon path: it resolves the path, applies migrations, merges, coerces, and returns an immutable `Settings` ([config.py:844](../../src/claire/config.py#L844), [daemon.py:1403](../../src/claire/daemon.py#L1403)).
- `load_config()` returns the raw merged `dict` instead of a `Settings` object, and is what callers use when they need the on-disk shape ([config.py:470](../../src/claire/config.py#L470)).
- `_save_json()` is the only writer: it writes a temp file in the target directory, `chmod 0o600`s it, and `os.replace`s it over the target, so a crash mid-write leaves the previous file intact ([config.py:361](../../src/claire/config.py#L361), [config.py:375](../../src/claire/config.py#L375), [config.py:378](../../src/claire/config.py#L378)).
- The desktop Settings window reads and writes `default_config_path()` and keeps `config.json` sparse: only values that differ from the defaults are written ([settings_window.py:401](../../src/desktop_app/settings_window.py#L401), [settings_window.py:906](../../src/desktop_app/settings_window.py#L906)).
- The panel reads the same default path for its config view ([server.py:1580](../../src/claire/panel/server.py#L1580), [server.py:2357](../../src/claire/panel/server.py#L2357)).

## 2. How defaults work

**One source, one object.** `get_default_config()` is the authoritative defaults dict, and `Settings` is a `@dataclass(frozen=True)` whose fields mirror it ([config.py:552](../../src/claire/config.py#L552), [config.py:86](../../src/claire/config.py#L86), [config.py:87](../../src/claire/config.py#L87)). `load_settings()` never mutates the dataclass; a change means loading again. The disk-format `ollama_*` keys remain readable as aliases and are promoted into the provider-aware `llm_*` / `embedding_*` keys during migration ([config.py:390](../../src/claire/config.py#L390), [config.py:411](../../src/claire/config.py#L411)).

**Coercion helpers.** Four normalisers are applied to merged values before `Settings` is built:

| Helper | Purpose | Anchor |
| :--- | :--- | :--- |
| `_ensure_list` | `None` → `[]`, comma string → list, scalar → one-element list | [config.py:489](../../src/claire/config.py#L489) |
| `_expand_path` | tilde-expand a path; `None`/`""`/`"null"` → `None` | [config.py:499](../../src/claire/config.py#L499) |
| `_expand_model_reference` | expand a value only when it is recognisably a path, so a Hugging Face repo id survives | [config.py:514](../../src/claire/config.py#L514) |
| `_ensure_dict` | dict passthrough; a `[{name: ...}]` list is keyed by `name` | [config.py:533](../../src/claire/config.py#L533) |

**Validation is per field, and it is not uniform.** Most numeric fields are converted with a bare `int()`/`float()` and no guard, so a non-numeric string raises out of `load_settings()` ([config.py:1076](../../src/claire/config.py#L1076) for `agentic_max_turns` is one example). A few numeric fields are wrapped in `try`/`except` and fall back to their default ([config.py:1087](../../src/claire/config.py#L1087), [config.py:1091](../../src/claire/config.py#L1091), [config.py:1097](../../src/claire/config.py#L1097), [config.py:953](../../src/claire/config.py#L953)). String-valued enums are coerced against an allow-list and silently replaced when unrecognised, for example `stt_engine` → `"parakeet"` ([config.py:1118](../../src/claire/config.py#L1118)–[config.py:1119](../../src/claire/config.py#L1119)) and `tool_selection_strategy` → `"llm"` ([config.py:1077](../../src/claire/config.py#L1077)). `tts_engine` is the deliberate exception: it is *not* coerced, so an unsupported engine reaches the factory and fails by name rather than being silently substituted ([config.py:945](../../src/claire/config.py#L945)).

**Unknown keys.** There is no unknown-key validation anywhere in the loader. A key the code does not know is kept in the merged dict that `load_config()` returns, and is simply never read when `Settings` is constructed field by field ([config.py:470](../../src/claire/config.py#L470), [config.py:1140](../../src/claire/config.py#L1140)). Nothing logs or reports it.

**A missing key is not an error.** Every key the loader reads has a default, so a sparse `config.json` is normal — it is the shape the Settings window writes ([settings_window.py:906](../../src/desktop_app/settings_window.py#L906)). A key whose *value* is semantically required (an empty `stt_model_path`, an unset voice id) fails later, in the consuming module, with a named error — see §7.

## 3. The generated example

`examples/config.json` is **generated**, not hand-written. It is produced by [scripts/generate_config_examples.py](../../scripts/generate_config_examples.py), which imports `export_example_config` ([generate_config_examples.py:17](../../scripts/generate_config_examples.py#L17)) and writes `examples/config.json` with `json.dump(..., indent=2)` plus a trailing newline ([generate_config_examples.py:20](../../scripts/generate_config_examples.py#L20)–[generate_config_examples.py:28](../../scripts/generate_config_examples.py#L28)). It is called with `include_db_path=False`, so `db_path` is rewritten from the machine-specific default to the portable `~/.local/share/claire/claire.db`; every other value is the default verbatim ([config.py:799](../../src/claire/config.py#L799)–[config.py:805](../../src/claire/config.py#L805)).

**The pinning test is [tests/test_examples_config_current.py](../../tests/test_examples_config_current.py).** `test_example_config_is_what_the_generator_writes` asserts the file equals the generator output exactly ([test_examples_config_current.py:128](../../tests/test_examples_config_current.py#L128)); `test_example_config_has_exactly_the_default_keys` pins the key set ([test_examples_config_current.py:77](../../tests/test_examples_config_current.py#L77)); `test_every_example_value_matches_the_current_default` pins the values ([test_examples_config_current.py:86](../../tests/test_examples_config_current.py#L86)). `db_path` is excluded from the generic comparison and asserted separately, precisely because it is the one key the generator rewrites ([test_examples_config_current.py:116](../../tests/test_examples_config_current.py#L116)).

**To regenerate**, from the repository root:

```sh
python scripts/generate_config_examples.py
```

The script inserts `src/` on `sys.path` itself, so no install or activation step is required ([generate_config_examples.py:12](../../scripts/generate_config_examples.py#L12)–[generate_config_examples.py:15](../../scripts/generate_config_examples.py#L15)). A new or renamed default key that is not regenerated fails the pinning test rather than shipping a stale example.

## 4. Secrets

No secret is ever a config value. The rule is that key material lives in the key store or in a `0600` file outside the repository, `llm_api_key` stays empty in `config.json`, and no value is ever printed, logged or echoed. No value is reproduced in this document.

- **The key store is the credentials owner.** `claire.platform.keys` stores every credential in a single `0600` JSON file outside the repository, created at `0600` from the moment of creation via `tempfile.mkstemp` and `os.replace`, with looser existing modes tightened and reported ([keys.py:1](../../src/claire/platform/keys.py#L1)–[keys.py:50](../../src/claire/platform/keys.py#L50), [keys.py:278](../../src/claire/platform/keys.py#L278)). Exactly one method returns a raw value; every other surface masks to the last four characters plus a SHA-256 fingerprint, and errors name the key and the path but never a value ([keys.py:203](../../src/claire/platform/keys.py#L203), [keys.py:665](../../src/claire/platform/keys.py#L665)).
- **The LLM key.** The default is the empty string, and the loader falls back to `OMNIROUTE_API_KEY` from the environment when the config value is empty — a non-empty `llm_api_key` in the file therefore *outranks* the environment ([config.py:901](../../src/claire/config.py#L901), [config.py:48](../../src/claire/config.py#L48)). The key store can also serve it through its environment-fallback table ([keys.py:119](../../src/claire/platform/keys.py#L119)).
- **The runtime env file.** `claire.env` is the `0600` file the systemd unit loads via `EnvironmentFile=`, and the path every "where do I put the key" message points at ([config.py:794](../../src/claire/config.py#L794), [service.py:404](../../src/claire/platform/service.py#L404)).
- **The Fish Audio key file.** `fish_key_file` names a key file outside the repository; the shipped default is the portable, unexpanded `~/.config/claire/fish-keys.txt`, which `load_settings()` expands at use time ([config.py:641](../../src/claire/config.py#L641), [config.py:979](../../src/claire/config.py#L979), [config.py:1293](../../src/claire/config.py#L1293)). The pool refuses a missing file, a non-file, and a file with any group/other bit set, each with a named error that carries the path ([fish_client.py:251](../../src/claire/output/fish_client.py#L251), [fish_client.py:260](../../src/claire/output/fish_client.py#L260)).
- **The config file is written `0600`**, because it can hold `llm_api_key`, `embedding_api_key` and `brave_search_api_key` ([config.py:352](../../src/claire/config.py#L352)). That is a file-mode guarantee, not a prohibition: the loader accepts a non-empty `llm_api_key` from the file, so the "never in `config.json`" rule is policy enforced by review and by the settings UI, not by the loader.

## 5. Key modules

| Module | Role | Anchors |
| :--- | :--- | :--- |
| [src/claire/config.py](../../src/claire/config.py) | Defaults, path resolution, load/merge/migrate, coercion, `Settings` | `_default_db_path` [67](../../src/claire/config.py#L67), `_default_vault_dir` [73](../../src/claire/config.py#L73), `Settings` [87](../../src/claire/config.py#L87), `default_config_path` [342](../../src/claire/config.py#L342), `_load_json` [349](../../src/claire/config.py#L349), `_save_json` [361](../../src/claire/config.py#L361), `_migrate_config` [399](../../src/claire/config.py#L399), `load_config` [470](../../src/claire/config.py#L470), `get_default_config` [552](../../src/claire/config.py#L552), `export_example_config` [799](../../src/claire/config.py#L799), `runtime_env_file` [808](../../src/claire/config.py#L808), `load_runtime_env` [826](../../src/claire/config.py#L826), `load_settings` [844](../../src/claire/config.py#L844) |
| [src/claire/platform/keys.py](../../src/claire/platform/keys.py) | Credential store: resolution, `0600` invariants, masking | `default_store_path` [278](../../src/claire/platform/keys.py#L278), `KeyNotFoundError` [203](../../src/claire/platform/keys.py#L203), raw accessor [665](../../src/claire/platform/keys.py#L665) |
| [src/claire/platform/service.py](../../src/claire/platform/service.py) | `0600` env-file path and the key-store/keyring self-check | `default_env_file` [404](../../src/claire/platform/service.py#L404), keyring probe [597](../../src/claire/platform/service.py#L597) |
| [src/desktop_app/settings_window.py](../../src/desktop_app/settings_window.py) | Graphical reader/writer of the config file | field metadata [360](../../src/desktop_app/settings_window.py#L360), path [401](../../src/desktop_app/settings_window.py#L401), non-default-only save [906](../../src/desktop_app/settings_window.py#L906) |
| [src/claire/panel/server.py](../../src/claire/panel/server.py) | Panel config view and config write | [1578](../../src/claire/panel/server.py#L1578), [2343](../../src/claire/panel/server.py#L2343) |
| [src/claire/cli.py](../../src/claire/cli.py) | Entry point; deliberately has no config flags | [19](../../src/claire/cli.py#L19)–[28](../../src/claire/cli.py#L28) |
| [scripts/generate_config_examples.py](../../scripts/generate_config_examples.py) | Generates the shipped example from the defaults | [17](../../scripts/generate_config_examples.py#L17), [20](../../scripts/generate_config_examples.py#L20) |

There is no `config.spec.md`; configuration behaviour is specified by the code above plus the per-module specs that consume it (for example [llm.spec.md](../../src/claire/llm/llm.spec.md), [stt.spec.md](../../src/claire/listening/stt/stt.spec.md), [keys.spec.md](../../src/claire/platform/keys.spec.md), [service.spec.md](../../src/claire/platform/service.spec.md)).

## 6. Extension points: adding a config key

Adding a key is a multi-file change; the worked shape is `vault_dir`, which touches each of the steps below.

1. **Declare the field** on `Settings`, in the right group, with a type ([config.py:87](../../src/claire/config.py#L87); `db_path` at [89](../../src/claire/config.py#L89) and `vault_dir` at [95](../../src/claire/config.py#L95)).
2. **Add the default** to `get_default_config()` ([config.py:552](../../src/claire/config.py#L552); `vault_dir` at [560](../../src/claire/config.py#L560)). This is the only place a default belongs.
3. **Normalise it in `load_settings()`** — path keys through `_expand_path`, list keys through `_ensure_list`, model-or-path keys through `_expand_model_reference`, maps through `_ensure_dict` — and pass it to the `Settings(...)` construction ([config.py:873](../../src/claire/config.py#L873), [config.py:1140](../../src/claire/config.py#L1140)).
4. **If it needs an environment override, add it deliberately.** `load_settings()` reads `os.environ` only for the four documented variables; a new env var is a new explicit read there, and if the fallback belongs next to the credential rather than the loader, extend `DEFAULT_ENV_SOURCES` in the key store instead ([keys.py:119](../../src/claire/platform/keys.py#L119)).
5. **Regenerate `examples/config.json`** with `scripts/generate_config_examples.py` — otherwise the pinning test fails (§3).
6. **Document the key** in [docs/CONFIGURATION.md](../CONFIGURATION.md) for its meaning, and, if it is worth naming in the short list, in the README key lists ([README.md:576](../../README.md#L576)).
7. **Add or update a test.** Key-specific invariants live in [tests/](../../tests); the example sync test covers the file surface, and loader behaviours have their own files (§8).
8. **Surface it in the desktop UI if it is user-facing** by extending the settings field metadata ([settings_window.py:360](../../src/desktop_app/settings_window.py#L360)).
9. **Update the consuming module's spec** when the change alters documented behaviour: `*.spec.md` files sit beside the code they describe and are authoritative ([AGENTS.md:15](../../AGENTS.md#L15)).

## 7. Failure modes

| Condition | What the code does | Anchor |
| :--- | :--- | :--- |
| Config file missing | `_load_json` returns `{}`, and the loader runs entirely on defaults. `load_settings()` creates the parent directory but never creates the file. | [config.py:349](../../src/claire/config.py#L349), [config.py:839](../../src/claire/config.py#L839) |
| Malformed JSON | Any exception while reading — parse error included — is swallowed and the *whole file* is discarded, with no warning: the process silently runs on defaults. | [config.py:349](../../src/claire/config.py#L349)–[config.py:358](../../src/claire/config.py#L358) |
| Wrong type, numeric field | Unguarded `int()`/`float()` raises out of `load_settings()` (for example `agentic_max_turns`); a few guarded fields fall back to their default. | [config.py:1076](../../src/claire/config.py#L1076), [config.py:1087](../../src/claire/config.py#L1087), [config.py:953](../../src/claire/config.py#L953) |
| Unrecognised string enum | Silently replaced with the safe default (`stt_engine` → `parakeet`, `tool_selection_strategy` → `llm`). `tts_engine` is the exception and raises by name downstream. | [config.py:1118](../../src/claire/config.py#L1118), [config.py:1077](../../src/claire/config.py#L1077), [config.py:945](../../src/claire/config.py#L945) |
| Unknown key | Kept by `load_config()`, ignored by `load_settings()`, never reported. | [config.py:470](../../src/claire/config.py#L470), [config.py:1140](../../src/claire/config.py#L1140) |
| Missing required key | Not possible from the file: every key has a default. Required *values* fail in the consumer — empty `stt_model_path` raises `SttModelMissingError`, an unknown `tts_engine` raises a `ValueError` naming the supported engines. | [parakeet.py:574](../../src/claire/listening/stt/parakeet.py#L574), [tts.py:1134](../../src/claire/output/tts.py#L1134) |
| Secret unresolvable | The LLM key becomes an empty string and the request goes out without an `Authorization` header; the endpoint rejects it. There is no config-level error for a missing `OMNIROUTE_API_KEY`. A key-store read instead raises a named error carrying the key name, store path and env var consulted, and a Fish key file raises named missing/empty/permission errors. | [config.py:915](../../src/claire/config.py#L915), [openai_compatible.py:114](../../src/claire/llm/openai_compatible.py#L114), [keys.py:665](../../src/claire/platform/keys.py#L665), [fish_client.py:251](../../src/claire/output/fish_client.py#L251) |
| Migration cannot be saved | A warning is printed and the migrated settings are still used in memory for this run. | [config.py:465](../../src/claire/config.py#L465) |
| `_save_json` fails | Returns `False`; the previous file is left untouched and no partial write survives. | [config.py:361](../../src/claire/config.py#L361), [config.py:378](../../src/claire/config.py#L378) |

## 8. Where its tests live

| Test file | What it pins |
| :--- | :--- |
| [tests/test_examples_config_current.py](../../tests/test_examples_config_current.py) | The generated example: exact generator output ([128](../../tests/test_examples_config_current.py#L128)), exact key set ([77](../../tests/test_examples_config_current.py#L77)), every value ([86](../../tests/test_examples_config_current.py#L86)), portable `db_path` ([116](../../tests/test_examples_config_current.py#L116)) and a portable `fish_key_file` ([176](../../tests/test_examples_config_current.py#L176)) |
| [tests/test_config_atomic_save.py](../../tests/test_config_atomic_save.py) | `_save_json` atomicity, temp-file cleanup and the `0600` mode |
| [tests/test_config_path_expansion.py](../../tests/test_config_path_expansion.py) | Tilde expansion of path-valued settings, and that a Hugging Face model id is not rewritten as a path |
| [tests/test_runtime_env_file.py](../../tests/test_runtime_env_file.py) | That `claire.env` is actually read, that a real environment variable outranks it, and that a missing file is not an error |
| [tests/test_config_models.py](../../tests/test_config_models.py) | Model/reference resolution through the loader |
| [tests/test_config_mcps.py](../../tests/test_config_mcps.py) | The `mcps` map and its list-of-pairs coercion |
| [tests/test_model_tiers.py](../../tests/test_model_tiers.py), [tests/test_provider_model_resolution.py](../../tests/test_provider_model_resolution.py) | Provider-aware key resolution after loading |
| [tests/test_llm_factory.py](../../tests/test_llm_factory.py) | Settings → backend construction, including the API-key path |
| [tests/test_platform_keys.py](../../tests/test_platform_keys.py) | Key-store round trip, `0600` invariants and masking |

Test selection and markers are configured in [pytest.ini](../../pytest.ini).

## 9. Documentation drift

Three contradictions between prose/specs and the code are visible in the tree. Each is stated from both sides; the code is the behaviour, the prose is the claim.

**9.1 `stt_engine: "whisper"` cannot select Whisper.**
The spec says Whisper is chosen by `stt_engine='faster_whisper'` or `'whisper'`, and that any other token raises `SttError` rather than quietly selecting an engine ([stt.spec.md:78](../../src/claire/listening/stt/stt.spec.md#L78)–[stt.spec.md:81](../../src/claire/listening/stt/stt.spec.md#L81)). The engine module accepts `"whisper"` ([stt/__init__.py:47](../../src/claire/listening/stt/__init__.py#L47)), the listener accepts it too ([listener.py:2037](../../src/claire/listening/listener.py#L2037)), and the intended `SttError` exists ([stt/__init__.py:81](../../src/claire/listening/stt/__init__.py#L81)). But the loader's allow-list omits it — `("parakeet", "faster_whisper")`, with every other token including `"whisper"` and the hyphenated `"faster-whisper"` rewritten to `"parakeet"` ([config.py:1118](../../src/claire/config.py#L1118)–[config.py:1120](../../src/claire/config.py#L1120)) — so through the daemon path ([daemon.py:1403](../../src/claire/daemon.py#L1403) → [listener.py:2039](../../src/claire/listening/listener.py#L2039)) `"whisper"` never reaches the engine and the `SttError` is unreachable.

**9.2 `CLAIRE_CONFIG_PATH` is honoured by the loader and ignored by the two UI writers.**
The README lists `CLAIRE_CONFIG_PATH` as the config-file override ([README.md:572](../../README.md#L572)), and the loader honours it ([config.py:477](../../src/claire/config.py#L477), [config.py:849](../../src/claire/config.py#L849)). The desktop Settings window resolves `default_config_path()` ([settings_window.py:401](../../src/desktop_app/settings_window.py#L401)), and the panel does the same ([server.py:1580](../../src/claire/panel/server.py#L1580), [server.py:2357](../../src/claire/panel/server.py#L2357)). `default_config_path()` reads only `XDG_CONFIG_HOME` ([config.py:342](../../src/claire/config.py#L342)–[config.py:346](../../src/claire/config.py#L346)), so with `CLAIRE_CONFIG_PATH` set the daemon reads one file while the Settings window and panel edit another.

**9.3 The documentation promises OS-keychain storage that the credential store does not implement.**
The cloud policy says keys live in the operating-system keychain where one is available, or in a `0600` file outside the repository ([CLOUD_POLICY.md:105](../CLOUD_POLICY.md#L105)–[CLOUD_POLICY.md:107](../CLOUD_POLICY.md#L107)), and the README describes `keyring` as a runtime dependency for moving away from plaintext values ([README.md:612](../../README.md#L612)). `claire.platform.keys` stores every credential in the `0600` JSON `keys.json` ([keys.py:278](../../src/claire/platform/keys.py#L278)) and imports no `keyring`; the only keyring use in `src/claire` is the self-check probe, which reports whether a backend is reachable ([service.py:597](../../src/claire/platform/service.py#L597)). `keyring` is a declared dependency ([requirements.txt:56](../../requirements.txt#L56)) but no key is stored in or read from the keychain.

**One previously-listed drift item no longer applies: the vault page exists.** `config.py` names `docs/OBSIDIAN.md` for the vault's configuration story ([config.py:94](../../src/claire/config.py#L94)), and the memory modules name it for layout and the no-writes-inside-a-repository rule ([notes.py:30](../../src/claire/memory/notes.py#L30), [obsidian.py:46](../../src/claire/memory/obsidian.py#L46)). That page exists ([docs/OBSIDIAN.md](../OBSIDIAN.md)) and describes the same vault layout the comments assume, so following the comment lands on the document rather than a dead name.

**The generated example ships only portable paths.** `db_path` is pinned as portable ([test_examples_config_current.py:122](../../tests/test_examples_config_current.py#L122)–[test_examples_config_current.py:124](../../tests/test_examples_config_current.py#L124)), and `_default_vault_dir` documents the same rule for anything shipped in the example ([config.py:77](../../src/claire/config.py#L77)–[config.py:82](../../src/claire/config.py#L82)). `fish_key_file` follows that rule too: its default is the unexpanded `~/.config/claire/fish-keys.txt` ([config.py:641](../../src/claire/config.py#L641), [examples/config.json:38](../../examples/config.json#L38)), which `load_settings()` expands at use time ([config.py:979](../../src/claire/config.py#L979), [config.py:1293](../../src/claire/config.py#L1293)), and `test_fish_key_file_default_is_portable` asserts it stays that way ([test_examples_config_current.py:176](../../tests/test_examples_config_current.py#L176)).

## 10. Related documents

- [docs/CONFIGURATION.md](../CONFIGURATION.md) — per-key prose and troubleshooting: model routes, speech recognition and TTS engines, MCP integrations, common issues. Start here for *what a key does*; this page answers *how configuration works*.
- [docs/CLOUD_POLICY.md](../CLOUD_POLICY.md) — what leaves the machine, and the secrets rules.
- [README.md](../../README.md#configuration) — the path/override table and the short list of keys worth knowing.
- [src/claire/platform/keys.spec.md](../../src/claire/platform/keys.spec.md) — the authoritative spec for credential storage.
