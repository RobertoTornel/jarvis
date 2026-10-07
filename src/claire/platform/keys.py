"""The Claire key store: credentials live outside the repository, always 0600.

Why this module exists
----------------------

Claire talks to OmniRouter, TTS engines and search providers that need a
credential.  Those credentials must not live in ``config.json`` (which is
copied, diffed and pasted into bug reports) and must not live in the
repository.  They live in one JSON file under the Claire config directory:

* ``$CLAIRE_KEY_STORE`` when set (explicit override, used by tests and ops),
* otherwise ``$XDG_CONFIG_HOME/claire/keys.json``,
* otherwise ``~/.config/claire/keys.json``.

The file is **never** inside the repository.

The four invariants this module is built around
-----------------------------------------------

1. **0600 from the moment of creation.**  Bytes are written into a
   ``tempfile.mkstemp`` handle — which the kernel creates mode 0600 — and the
   handle is then ``os.replace``\\ d into place.  There is no window in which a
   world-readable file holds a key, which is exactly what a
   "write then chmod" would create.
2. **Looser existing permissions are tightened and reported.**  An existing
   store file with any group/other bit set is ``chmod 0600``\\ ed *and* the
   tightening is reported three ways: a ``WARNING`` log line naming the path and
   both modes, a :class:`PermissionReport` appended to
   :attr:`KeyStore.tightened_permissions`, and an optional ``on_tighten``
   callback.  If the mode cannot be brought to 0600 the call raises
   :class:`KeyStorePermissionError` rather than proceeding anyway.
3. **Masking is the default everywhere.**  Exactly one method returns a raw
   stored value: :meth:`KeyStore.get`, the deliberate accessor the rest of the
   app calls when it actually needs to use a key.  Every other surface —
   :meth:`KeyStore.render`, :meth:`KeyStore.describe`,
   :class:`KeyRecord`, ``__repr__``, log lines, exception messages — is masked
   to last-4 plus a truncated SHA-256 fingerprint, and error messages name the
   key *name* and never a value.
4. **A missing key is a named error, never a silent no-op.**  The error names
   the key, the store path and the environment variable that was consulted, and
   carries the exact command that would fix it.

Optional environment fallback
-----------------------------

:meth:`KeyStore.get` and :meth:`KeyStore.render` can fall back to an
environment variable when the store has no entry, so an existing
``export OMNIROUTE_API_KEY=...`` keeps working.  The *source* that won is
reported (``"store"`` or ``"env:OMNIROUTE_API_KEY"``); the value never is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

_log = logging.getLogger(__name__)

__all__ = [
    "KeyStore",
    "KeyRecord",
    "PermissionReport",
    "KeyStoreError",
    "KeyStoreCorruptError",
    "KeyNotFoundError",
    "KeyStorePermissionError",
    "KeyNameError",
    "KeyValueError",
    "STORE_FILE_MODE",
    "FINGERPRINT_LENGTH",
    "REVEAL_MIN_LENGTH",
    "DEFAULT_ENV_SOURCES",
    "RAW_VALUE_ACCESSOR",
    "default_store_path",
    "fingerprint",
    "mask",
]


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: The only mode a key store file is ever allowed to have.
STORE_FILE_MODE = 0o600

#: The only mode the dedicated Claire config directory is created with.
STORE_DIR_MODE = 0o700

#: Any of these bits means "someone other than the owner can read this file".
_LOOSE_BITS = 0o077

#: Characters of the SHA-256 hex digest shown in a masked rendering.
FINGERPRINT_LENGTH = 8

#: Values shorter than this reveal *no* characters, not even the last four,
#: because "last 4 of 6" is most of the secret.  At or above it the rendering is
#: last-4 plus the fingerprint, as specified.
REVEAL_MIN_LENGTH = 8

#: Value characters kept in a masked rendering for a long enough value.
REVEAL_LAST = 4

#: Maximum accepted key-name length.
MAX_NAME_LENGTH = 128

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,%d}" % (MAX_NAME_LENGTH - 1))

#: Environment variables to "a key name in this store -> the variable to consult
#: when the store has no entry" — the optional fallback source.  Extend this
#: table rather than hard-coding ``os.environ`` lookups at a call site.
DEFAULT_ENV_SOURCES: Dict[str, str] = {
    # Claire's default provider (OmniRouter at 127.0.0.1:20129) reads its key
    # from the environment today (see ``claire.config.OMNIROUTE_KEY_ENV``).
    "OMNIROUTE_API_KEY": "OMNIROUTE_API_KEY",
}

#: The single member of this module that returns an unmasked stored value.
#: Enforced by ``tests/test_platform_keys.py``, which sweeps every other
#: surface for the value and parses this module to check that no other
#: function returns a subscript into the stored mapping.
RAW_VALUE_ACCESSOR = "KeyStore.get"


# --------------------------------------------------------------------------
# Masking
# --------------------------------------------------------------------------

def fingerprint(value: str, length: int = FINGERPRINT_LENGTH) -> str:
    """Return a short, stable, non-reversible fingerprint of ``value``.

    A truncated SHA-256 hex digest.  It is enough to tell two keys apart in a
    log line and useless for recovering the key.
    """
    if not isinstance(value, str):
        raise TypeError("fingerprint() takes a str")
    if length < 1:
        raise ValueError("fingerprint length must be >= 1")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def mask(value: str, *, fingerprint_length: int = FINGERPRINT_LENGTH) -> str:
    """Return the canonical masked rendering: last-4 plus a fingerprint.

    ``"sk-abcdefghijklmnop1234"`` renders as ``"...1234#<8 hex>"``.  A value
    shorter than :data:`REVEAL_MIN_LENGTH` reveals no characters at all
    (``"****#<8 hex>"``) because four characters of a six-character secret is
    most of the secret.

    The full value never appears in the result, and the result is safe to put
    in a log line, an exception message or a ``repr``.
    """
    if not isinstance(value, str):
        raise TypeError("mask() takes a str")
    digest = fingerprint(value, fingerprint_length)
    if len(value) >= REVEAL_MIN_LENGTH:
        return f"...{value[-REVEAL_LAST:]}#{digest}"
    return f"{'*' * REVEAL_LAST}#{digest}"


# --------------------------------------------------------------------------
# Named errors — every one names the key (never a value) and a fix
# --------------------------------------------------------------------------

class KeyStoreError(Exception):
    """Base class for every key-store failure.  Never carries a value."""


class KeyNameError(KeyStoreError):
    """A key name was empty or contained characters that cannot be a key name."""


class KeyValueError(KeyStoreError):
    """A value was empty, not a string, or contained a newline.

    Raised instead of storing something unusable, so a bad paste fails loudly
    rather than authenticating as nobody six hours later.
    """


class KeyStorePermissionError(KeyStoreError):
    """The store file could not be kept at 0600."""


class KeyStoreCorruptError(KeyStoreError):
    """The store file is not valid JSON or not the expected shape.

    Deliberately reports the path and the *kind* of problem only: a
    ``json.JSONDecodeError`` message quotes the offending document, which is
    the key store, so the original exception is never chained or interpolated.
    """


class KeyNotFoundError(KeyStoreError):
    """A requested key is in neither the store nor its fallback environment.

    The message names the key, the store path and the environment variable that
    was consulted — never a value.
    """

    def __init__(self, name: str, *, path: "os.PathLike[str] | str | None" = None,
                 env_var: Optional[str] = None) -> None:
        self.name = name
        self.path = str(path) if path is not None else None
        self.env_var = env_var
        where = f" (store file: {self.path})" if self.path else ""
        if env_var:
            missing = f"and the fallback environment variable {env_var} is unset or empty"
            fix = (
                f"Fix: run `claire keys set {name}`, or export {env_var}=<your key>."
            )
        else:
            missing = "and no fallback environment variable is configured for it"
            fix = f"Fix: run `claire keys set {name}`."
        super().__init__(
            f"key {name!r} is not in the Claire key store{where} {missing}. {fix}"
        )


# --------------------------------------------------------------------------
# Report / record value objects — masked by construction
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PermissionReport:
    """Evidence that a looser mode was tightened.  Contains no key material."""

    path: str
    previous_mode: int
    new_mode: int

    def __str__(self) -> str:
        return (
            f"tightened {self.path} from {oct(self.previous_mode)} "
            f"to {oct(self.new_mode)}"
        )

    __repr__ = __str__


@dataclass(frozen=True)
class KeyRecord:
    """A masked description of one stored key.

    This is the only "value object" this module hands out, and it holds no raw
    value: ``masked`` is a last-4-plus-fingerprint rendering and ``fingerprint``
    is a truncated digest.  ``repr`` is therefore safe by construction.
    """

    name: str
    masked: str
    fingerprint: str
    source: str = "store"

    def __repr__(self) -> str:
        return (
            f"KeyRecord(name={self.name!r}, masked={self.masked!r}, "
            f"source={self.source!r})"
        )

    def __str__(self) -> str:
        return f"{self.name}: {self.masked} ({self.source})"


# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------

def default_store_path() -> Path:
    """Resolve the store path outside the repository.

    ``$CLAIRE_KEY_STORE`` wins, then ``$XDG_CONFIG_HOME/claire/keys.json``,
    then ``~/.config/claire/keys.json``.  This mirrors
    ``claire.config.default_config_path()`` so the store sits next to the
    config file the user already knows.
    """
    override = os.environ.get("CLAIRE_KEY_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "claire" / "keys.json"


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

def _validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise KeyNameError(
            f"key names must be strings, got {type(name).__name__}"
        )
    cleaned = name.strip()
    if not cleaned:
        raise KeyNameError(
            "key names must not be empty. Fix: pass a real name, e.g. "
            "KeyStore.store('OMNIROUTE_API_KEY', <value>)."
        )
    if not _NAME_RE.fullmatch(cleaned):
        raise KeyNameError(
            f"invalid key name {cleaned!r}: names are 1-{MAX_NAME_LENGTH} "
            "characters of [A-Za-z0-9_.-] and must start alphanumeric. "
            "Fix: use an environment-variable style name, e.g. "
            "'OMNIROUTE_API_KEY'."
        )
    return cleaned


def _validate_value(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise KeyValueError(
            f"value for key {name!r} must be a string, got {type(value).__name__}. "
            "Fix: pass str(value) explicitly so the caller decides the encoding."
        )
    cleaned = value.strip()
    if not cleaned:
        raise KeyValueError(
            f"value for key {name!r} is empty. Fix: pass the real credential, or "
            f"delete the key with KeyStore.delete({name!r}) — an empty key would "
            "be stored and then fail authentication as a silent no-op."
        )
    if "\n" in cleaned or "\r" in cleaned:
        raise KeyValueError(
            f"value for key {name!r} contains a newline, which no credential "
            "contains. Fix: read the value without the trailing newline, e.g. "
            "open(path).read().strip()."
        )
    return cleaned


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

class KeyStore:
    """A 0600 JSON key store outside the repository.

    Parameters
    ----------
    path:
        Store file.  Defaults to :func:`default_store_path`.
    env:
        Environment mapping for the optional fallback source.  Defaults to
        ``os.environ`` *at call time*, so tests can redirect it.
    on_tighten:
        Called with each :class:`PermissionReport` when an existing file had to
        be tightened — the third, explicit reporting channel.
    """

    def __init__(
        self,
        path: "os.PathLike[str] | str | None" = None,
        *,
        env: Optional[Mapping[str, str]] = None,
        on_tighten: Optional[Callable[[PermissionReport], None]] = None,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else default_store_path()
        self._env_override = env
        self._on_tighten = on_tighten
        self._tightened: List[PermissionReport] = []
        self._dir_note_logged = False
        self._lock = threading.RLock()
        self._ensure_dir()
        # Tighten an existing loose file now, on construction, so the report
        # exists before any key is read or written and cannot be missed.
        self._check_file()

    # -- introspection ----------------------------------------------------

    @property
    def env(self) -> Mapping[str, str]:
        """The environment consulted for fallbacks (``os.environ`` by default)."""
        return self._env_override if self._env_override is not None else os.environ

    @property
    def tightened_permissions(self) -> Tuple[PermissionReport, ...]:
        """Every tightening performed by this instance, oldest first."""
        return tuple(self._tightened)

    def __repr__(self) -> str:
        # Names only.  Never values, never masked renderings either — a repr is
        # the most likely thing to be pasted into a bug report.
        try:
            names = self.list_names()
        except KeyStoreError:
            names = []
        return f"KeyStore(path={str(self.path)!r}, names={names!r})"

    __str__ = __repr__

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        try:
            self.source_of(name)
        except KeyStoreError:
            return False
        return True

    def __len__(self) -> int:
        return len(self.list_names())

    def __iter__(self) -> Iterator[str]:
        return iter(self.list_names())

    # -- layout / permissions --------------------------------------------

    def _ensure_dir(self) -> None:
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True, mode=STORE_DIR_MODE)
            try:
                os.chmod(parent, STORE_DIR_MODE)
            except OSError:
                # The file itself is what must be 0600, and that is enforced on
                # every operation; a directory whose mode could not be set is
                # not worth failing the whole store over.
                pass
            return
        # An existing parent directory is the user's to own: if it is
        # group/other-accessible, note it once (so the situation is never
        # invisible) but do not chmod a directory Claire shares with other
        # tools.  The store file inside it is 0600 either way.
        if self._dir_note_logged:
            return
        self._dir_note_logged = True
        try:
            mode = stat.S_IMODE(os.stat(parent).st_mode)
        except OSError:
            return
        if mode & _LOOSE_BITS:
            _log.info(
                "key store directory %s has mode %s; the store file itself is "
                "kept at 0600 regardless",
                parent,
                oct(mode),
            )

    def _tighten_file(self, path: Path) -> Optional[PermissionReport]:
        """Bring an existing ``path`` to 0600, reporting the change.

        Returns ``None`` when the file was already private.  Raises
        :class:`KeyStorePermissionError` when it could not be made private —
        proceeding would be the silent leak this module exists to prevent.
        """
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as exc:
            raise KeyStorePermissionError(
                f"cannot stat the key store {path}: {exc.strerror or exc}. "
                f"Fix: check the path exists and is readable, or delete it and "
                f"store the key again."
            ) from None

        if not (mode & _LOOSE_BITS):
            # Already private (0600, 0400, ...). Nothing to do, nothing to say.
            return None

        try:
            os.chmod(path, STORE_FILE_MODE)
        except OSError as exc:
            raise KeyStorePermissionError(
                f"key store {path} has mode {oct(mode)}, which lets other users "
                f"read your credentials, and it could not be tightened to 0600 "
                f"({exc.strerror or exc}). Fix: run `chmod 600 {path}` yourself, "
                f"or delete the file and store the key again."
            ) from None

        try:
            new_mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            new_mode = -1
        if new_mode & _LOOSE_BITS:
            raise KeyStorePermissionError(
                f"key store {path} still has mode {oct(new_mode)} after chmod; "
                f"the filesystem appears to ignore file modes. Fix: run "
                f"`chmod 600 {path}` yourself and check the filesystem honours "
                f"POSIX permissions, or move the store to one that does."
            )

        report = PermissionReport(str(path), mode, new_mode)
        self._tightened.append(report)
        _log.warning(
            "key store permissions were too loose and have been tightened: %s", report
        )
        if self._on_tighten is not None:
            try:
                self._on_tighten(report)
            except Exception:  # a reporting hook must never break the store
                _log.exception("on_tighten callback failed (mode already fixed)")
        return report

    def _check_file(self) -> None:
        """Tighten the existing store file if needed (idempotent)."""
        if self.path.exists():
            self._tighten_file(self.path)

    # -- persistence ------------------------------------------------------

    def _read_file(self) -> Dict[str, str]:
        """Read the raw mapping.  Internal: the storage layer, not an API.

        Callers outside this module must use :meth:`get`, whose result is the
        only raw value the rest of the app ever handles.
        """
        self._check_file()
        if not self.path.exists():
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise KeyStoreError(
                f"cannot read the key store {self.path}: {exc.strerror or exc}. "
                f"Fix: check permissions on the file and its directory."
            ) from None
        except UnicodeDecodeError:
            raise KeyStoreCorruptError(
                f"key store {self.path} is not valid UTF-8. Fix: delete the file "
                f"and store the key again."
            ) from None

        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            # Never interpolate the decoder message: it quotes the document,
            # and the document is the key store.
            raise KeyStoreCorruptError(
                f"key store {self.path} is not valid JSON. Fix: delete the file "
                f"and store the key again, or repair it by hand."
            ) from None

        if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
            raise KeyStoreCorruptError(
                f"key store {self.path} has an unexpected shape (expected "
                f'{{"version": 1, "keys": {{name: value}}}}). Fix: delete the '
                f"file and store the key again."
            )

        keys: Dict[str, str] = {}
        for name, value in data["keys"].items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise KeyStoreCorruptError(
                    f"key store {self.path} contains a non-string entry. Fix: "
                    f"delete the file and store the key again."
                )
            keys[name] = value
        return keys

    def _write_file(self, keys: Mapping[str, str]) -> None:
        """Atomically replace the store with ``keys``, created 0600.

        The bytes go to an ``mkstemp`` handle (mode 0600 from creation) which is
        then ``os.replace``\\ d over the target.  At no point does a
        world-readable file exist.
        """
        self._ensure_dir()
        payload = json.dumps(
            {"version": 1, "keys": dict(keys)}, indent=2, sort_keys=True
        ) + "\n"

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            # mkstemp already creates 0600; state it explicitly so the intent
            # survives a future refactor to a different temp-file helper.
            try:
                os.fchmod(fd, STORE_FILE_MODE)
            except (AttributeError, OSError):
                os.chmod(tmp_path, STORE_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            observed = stat.S_IMODE(os.stat(tmp_path).st_mode)
            if observed & _LOOSE_BITS:
                raise KeyStorePermissionError(
                    f"refusing to write the key store through temp file "
                    f"{tmp_path} with mode {oct(observed)}: a credential must "
                    f"never exist in a group- or world-readable file, not even "
                    f"briefly. Fix: check the temp directory's umask settings."
                )
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

        # os.replace preserves the temp file's mode, but verify rather than
        # assume: a store that silently ended up readable is the failure this
        # module exists to prevent.
        final = stat.S_IMODE(os.stat(self.path).st_mode)
        if final & _LOOSE_BITS:
            raise KeyStorePermissionError(
                f"key store {self.path} ended up with mode {oct(final)} after an "
                f"atomic write. Fix: run `chmod 600 {self.path}` and check the "
                f"filesystem honours POSIX file modes."
            )

    # -- the API ----------------------------------------------------------

    def store(self, name: str, value: str) -> KeyRecord:
        """Store ``value`` under ``name`` and return a masked record.

        The returned :class:`KeyRecord` is the safe handle for logging: it
        carries the masked rendering and the fingerprint, never the value.
        """
        key = _validate_name(name)
        secret = _validate_value(key, value)
        with self._lock:
            keys = self._read_file()
            keys[key] = secret
            self._write_file(keys)
        record = KeyRecord(
            name=key,
            masked=mask(secret),
            fingerprint=fingerprint(secret),
            source="store",
        )
        _log.info("stored key %s (%s) in %s", key, record.masked, self.path)
        return record

    def get(self, name: str, *, env_var: Optional[str] = None) -> str:
        """Return the **raw** value for ``name``.

        This is the one and only function in this module that returns an
        unmasked stored value: it is the deliberate accessor the rest of the
        application calls when it actually needs to *use* a credential (for
        example to build an ``Authorization`` header).  Do not log, format or
        return its result anywhere a human or a file can see it.

        Falls back to an environment variable when the store has no entry —
        ``env_var`` if given, else :data:`DEFAULT_ENV_SOURCES` for ``name``.
        Use :meth:`source_of` to learn which source won without touching the
        value.

        Raises :class:`KeyNotFoundError` naming the key when neither source has
        it.
        """
        key = _validate_name(name)
        resolved_from = env_var if env_var is not None else DEFAULT_ENV_SOURCES.get(key)
        with self._lock:
            keys = self._read_file()
        if key in keys:
            return keys[key]
        if resolved_from:
            raw = self.env.get(resolved_from, "")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        raise KeyNotFoundError(key, path=self.path, env_var=resolved_from)

    def source_of(self, name: str, *, env_var: Optional[str] = None) -> str:
        """Report which source *would* supply ``name``: ``"store"`` or ``"env:VAR"``.

        Reports the source only — never the value.
        """
        key = _validate_name(name)
        resolved_from = env_var if env_var is not None else DEFAULT_ENV_SOURCES.get(key)
        with self._lock:
            keys = self._read_file()
        if key in keys:
            return "store"
        if resolved_from:
            raw = self.env.get(resolved_from, "")
            if isinstance(raw, str) and raw.strip():
                return f"env:{resolved_from}"
        raise KeyNotFoundError(key, path=self.path, env_var=resolved_from)

    def render(self, name: str, *, env_var: Optional[str] = None) -> str:
        """Return the masked rendering of ``name``'s value, plus its source.

        ``"...1234#a1b2c3d4 (store)"`` or ``"...1234#a1b2c3d4 (env:OMNIROUTE_API_KEY)``.
        Safe for a log line, a UI or an error message.  Raises
        :class:`KeyNotFoundError` when neither source has the key.
        """
        key = _validate_name(name)
        resolved_from = env_var if env_var is not None else DEFAULT_ENV_SOURCES.get(key)
        with self._lock:
            keys = self._read_file()
        if key in keys:
            return f"{mask(keys[key])} (store)"
        if resolved_from:
            raw = self.env.get(resolved_from, "")
            if isinstance(raw, str) and raw.strip():
                return f"{mask(raw.strip())} (env:{resolved_from})"
        raise KeyNotFoundError(key, path=self.path, env_var=resolved_from)

    def delete(self, name: str) -> bool:
        """Remove ``name``.  Returns ``True`` if it was there, ``False`` if not.

        A delete of an absent key is idempotent, not an error, but it is never
        silent: the boolean says which happened and the log line names the
        outcome.
        """
        key = _validate_name(name)
        with self._lock:
            keys = self._read_file()
            if key not in keys:
                _log.info("key %s was not present in %s; nothing deleted", key, self.path)
                return False
            del keys[key]
            self._write_file(keys)
        _log.info("deleted key %s from %s", key, self.path)
        return True

    def list_names(self) -> List[str]:
        """Every key name in the store, sorted.  Names only — never values."""
        with self._lock:
            keys = self._read_file()
        return sorted(keys)

    def describe(self) -> List[KeyRecord]:
        """Masked descriptions of every stored key, sorted by name.

        Masked renderings only; there is no unmasked counterpart to this method
        by design.
        """
        with self._lock:
            keys = self._read_file()
        records = [
            KeyRecord(
                name=name,
                masked=mask(value),
                fingerprint=fingerprint(value),
                source="store",
            )
            for name, value in keys.items()
        ]
        records.sort(key=lambda record: record.name)
        return records
