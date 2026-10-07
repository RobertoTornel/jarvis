## Platform Key Store Spec

This specification documents `src/claire/platform/keys.py` — the single place Claire keeps
credentials (OmniRouter keys, TTS keys, search-provider keys). Its whole reason to exist is that
credentials must never be in the repository, never in `config.json` (which is copied into bug
reports), never world-readable, and never printed.

### Where the file lives

| Resolution order | Value |
|---|---|
| 1 | `$CLAIRE_KEY_STORE` (explicit override; used by tests and ops) |
| 2 | `$XDG_CONFIG_HOME/claire/keys.json` |
| 3 | `~/.config/claire/keys.json` |

`default_store_path()` implements the table; it mirrors `claire.config.default_config_path()` so
the store sits beside the config file the user already knows. **The store is never inside the
repository** and the module never writes to a path under the checkout unless the caller passes one
explicitly.

On-disk format:

```json
{
  "version": 1,
  "keys": { "OMNIROUTE_API_KEY": "<raw value>" }
}
```

The dedicated config directory is *created* 0700 (`STORE_DIR_MODE`). An existing
parent directory is never chmod-ed — it may be shared with other tools — but a loose mode is
noted once at INFO and the store file inside it is still kept at 0600.

### Invariants

**1. Always 0600, from the moment of creation.**
`_write_file()` serialises to JSON, writes it into a `tempfile.mkstemp` handle — which the kernel
creates mode 0600 — and `os.replace`s that handle over the store. There is no window in which a
group- or world-readable file holds a key. A "write, then `chmod`" is explicitly forbidden: that
window *is* the leak. After the replace the final mode is re-`stat`ed and the write raises
`KeyStorePermissionError` if any group/other bit survived.

**2. Looser existing permissions are tightened and reported.**
If the existing file has any group/other bit set, `_tighten_file()` `chmod 0600`s it and reports
the change through **three** channels before anything else happens:

* a `WARNING` log line naming the path and both modes,
* a `PermissionReport` appended to `KeyStore.tightened_permissions`,
* the optional `on_tighten` callback passed to the constructor.

If the mode cannot be brought to 0600 (filesystem ignores modes), the call raises
`KeyStorePermissionError` with the exact `chmod 600 <path>` fix — it never proceeds anyway.

**3. Masking is the default; exactly one raw accessor.**
`KeyStore.get(name)` is the **one and only** function in the module that returns an unmasked
stored value. It is documented as the deliberate accessor the rest of the app calls when it
actually has to build an `Authorization` header. Every other surface is masked:

| Surface | Returns |
|---|---|
| `KeyStore.get(name)` | **raw** value (the one exception) |
| `KeyStore.render(name)` | `"...1234#a1b2c3d4 (store)"` / `"...1234#a1b2c3d4 (env:OMNIROUTE_API_KEY)"` |
| `KeyStore.describe()` | `KeyRecord` list — masked rendering + fingerprint only |
| `KeyStore.list_names()` | names only |
| `KeyRecord` / `PermissionReport` | masked by construction; no field can hold a value |
| `KeyStore.__repr__` / `__str__` | path + names, no values |

Masking format (`mask()`): the last 4 characters plus `#` plus the first 8 hex characters of the
SHA-256 of the value. A value shorter than `REVEAL_MIN_LENGTH` (8) reveals **no** characters at
all — `"****#a1b2c3d4"` — because the last 4 of a 6-character secret is most of the secret.

Values never reach a log line, an exception message or a `repr`. Corrupt-file errors deliberately
report only the path and the *kind* of problem: `json.JSONDecodeError` quotes the offending
document, and the document is the key store, so the decoder message is never chained or
interpolated.

**4. A missing key is a named error, never a silent no-op.**
`KeyNotFoundError` names the key, the store path, the fallback environment variable that was
consulted, and the fix (`claire keys set <NAME>`, or `export VAR=<your key>`). It never names a
value. `store()` rejects empty/blank/non-string/newline values with `KeyValueError` and a fix,
so a bad paste fails loudly instead of authenticating as nobody six hours later.

**5. Atomic writes.**
Temp file in the store's own directory (same filesystem, so `os.replace` is atomic), `fsync`
before the swap, temp file unlinked on any failure. A crash mid-write leaves either the old store
or the new one, never a truncated file, and never a stray `.<name>.*.tmp`.

### API

| Member | Behaviour |
|---|---|
| `KeyStore(path=None, *, env=None, on_tighten=None)` | `path` defaults to `default_store_path()`; `env` defaults to `os.environ` **at call time**; `on_tighten(report)` is the explicit reporting hook. |
| `store(name, value) -> KeyRecord` | Validates, merges into the existing mapping, atomic 0600 write. Returns a masked record safe to log. |
| `get(name, *, env_var=None) -> str` | The raw accessor. Store wins; else `env_var` (or `DEFAULT_ENV_SOURCES[name]`). Raises `KeyNotFoundError`. |
| `source_of(name, *, env_var=None) -> str` | `"store"` or `"env:VAR"` — which source *would* win, without the value. |
| `render(name, *, env_var=None) -> str` | Masked rendering + winning source. Raises `KeyNotFoundError`. |
| `delete(name) -> bool` | `True` if removed, `False` if it was absent (idempotent, logged, never silent). |
| `list_names() -> list[str]` | Sorted names, names only. |
| `describe() -> list[KeyRecord]` | Sorted masked records. |
| `tightened_permissions -> tuple[PermissionReport, ...]` | Evidence of every tightening this instance performed. |
| `__repr__`, `__str__`, `__len__`, `__iter__`, `__contains__` | Names only; `in` is true when either source can supply the key. |

### Optional environment fallback

`DEFAULT_ENV_SOURCES` maps a store key name to the environment variable to consult when the store
has no entry. It ships with `OMNIROUTE_API_KEY -> OMNIROUTE_API_KEY` so an existing
`export OMNIROUTE_API_KEY=...` (the value `claire.config` reads today) keeps working after the
store is introduced. The winning source is reported by `source_of()` and embedded in `render()`
— the value itself is never reported.

### Errors

| Error | Raised when | Names |
|---|---|---|
| `KeyNameError` | name empty, non-string, or outside `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}` | the offending name |
| `KeyValueError` | value empty/blank, non-string, or contains a newline | the key name |
| `KeyStoreCorruptError` | file is not UTF-8, not valid JSON, or not the expected shape | the path, never the contents |
| `KeyStorePermissionError` | mode is loose and cannot be tightened, or a write would leave a loose file | the path and both modes |
| `KeyNotFoundError` | `get`/`render`/`source_of` find it in neither source | the key name, the path, the env var, the fix |

All of them subclass `KeyStoreError`.

### Intended call sites (not yet wired)

`claire.config` currently reads the OmniRouter key from `OMNIROUTE_API_KEY` directly. The intended
migration is `KeyStore().get("OMNIROUTE_API_KEY")` so a key can be stored once with
`claire keys set OMNIROUTE_API_KEY` and still be overridable by the environment. Call sites must
use `get()` only for the moment of use, and `render()`/`describe()` for anything a human reads.
No call site may log, format or persist a `get()` result.

### Tests

`tests/test_platform_keys.py` (all marked `pytestmark = pytest.mark.unit`): round-trip store/get,
0600 at creation as observed through `os.replace`'s source file, tightening of a pre-existing
0644 file *and* the report of it, masking (value absent from `render`, `repr()`, and captured log
output), delete, `list_names`, missing-name error naming the name, short-value over-masking,
corrupt-file errors that do not quote the file, and the atomic-write guarantee (no temp file left,
no broadly readable temp file ever).
