"""Tests for the Claire key store (``src/claire/platform/keys.py``).

The store holds live credentials, so the tests here are mostly about what must
*not* happen: no value in a rendering, a repr, a log line or an exception
message; no world-readable file at any instant, including the instant between
creation and ``chmod``; no silent no-op when a key is missing.

Every test redirects the config dir into ``tmp_path`` (``XDG_CONFIG_HOME`` plus
an explicit path), so no test can touch the real ``~/.config/claire/keys.json``.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import logging
import os
import stat
import tempfile
from pathlib import Path

import pytest

from claire.platform import keys
from claire.platform.keys import (
    KeyNameError,
    KeyNotFoundError,
    KeyRecord,
    KeyStore,
    KeyStoreCorruptError,
    KeyStoreError,
    KeyStorePermissionError,
    KeyValueError,
    PermissionReport,
    default_store_path,
    fingerprint,
    mask,
)

pytestmark = pytest.mark.unit


#: A value shaped like a real credential.  Its last four characters are
#: ``1234`` (which the masking contract *does* render) and its middle is a
#: distinctive chunk that must never survive any rendering.
SECRET = "sk-omniroute-9f4c2a7be15d8033c0ffee1234"
SECRET_HEAD = "sk-omniroute"
SECRET_MID = "9f4c2a7be15d8033c0ffee"
SECRET_TAIL = "1234"
SECRET_FINGERPRINT = hashlib.sha256(SECRET.encode("utf-8")).hexdigest()[:8]

OTHER_SECRET = "tts-provider-00112233445566778899-9999"

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Redirect every config-dir resolution into ``tmp_path``.

    Also clears the two environment variables the store consults, so a
    developer's exported ``OMNIROUTE_API_KEY`` cannot change a result.
    """
    home = tmp_path / "xdg"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.delenv("CLAIRE_KEY_STORE", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    return home


@pytest.fixture
def store_path(cfg_home):
    """The default store location under the redirected config dir."""
    return cfg_home / "claire" / "keys.json"


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# Round trip and basic API
# ---------------------------------------------------------------------------

def test_store_get_round_trip(store_path):
    store = KeyStore(store_path)
    record = store.store("OMNIROUTE_API_KEY", SECRET)

    assert store.get("OMNIROUTE_API_KEY") == SECRET
    assert record.name == "OMNIROUTE_API_KEY"

    # It survived a fresh instance, i.e. it is really on disk.
    assert KeyStore(store_path).get("OMNIROUTE_API_KEY") == SECRET


def test_store_returns_masked_record_not_the_value(store_path):
    store = KeyStore(store_path)
    record = store.store("OMNIROUTE_API_KEY", SECRET)

    assert isinstance(record, KeyRecord)
    assert SECRET not in repr(record)
    assert SECRET_MID not in repr(record)
    assert record.masked == f"...{SECRET_TAIL}#{SECRET_FINGERPRINT}"
    assert record.fingerprint == SECRET_FINGERPRINT


def test_store_trims_surrounding_whitespace(store_path):
    """A pasted key with a trailing newline must not be stored with it."""
    store = KeyStore(store_path)
    store.store("OMNIROUTE_API_KEY", f"  {SECRET}\n")

    assert store.get("OMNIROUTE_API_KEY") == SECRET


def test_store_overwrites_existing_key(store_path):
    store = KeyStore(store_path)
    store.store("K", SECRET)
    store.store("K", OTHER_SECRET)

    assert store.get("K") == OTHER_SECRET
    assert store.list_names() == ["K"]


def test_list_names_is_sorted_and_names_only(store_path):
    store = KeyStore(store_path)
    store.store("ZED_KEY", SECRET)
    store.store("ALPHA_KEY", OTHER_SECRET)

    assert store.list_names() == ["ALPHA_KEY", "ZED_KEY"]
    assert len(store) == 2
    assert sorted(iter(store)) == ["ALPHA_KEY", "ZED_KEY"]
    assert "ALPHA_KEY" in store


def test_delete_removes_key_and_reports_absence(store_path, caplog):
    store = KeyStore(store_path)
    store.store("OMNIROUTE_API_KEY", SECRET)

    with caplog.at_level(logging.DEBUG, logger="claire.platform.keys"):
        assert store.delete("OMNIROUTE_API_KEY") is True
        # Deleting an absent key is idempotent, but never silent: it says so.
        assert store.delete("OMNIROUTE_API_KEY") is False

    assert store.list_names() == []
    assert "not present" in caplog.text
    with pytest.raises(KeyNotFoundError):
        store.get("OMNIROUTE_API_KEY")


def test_delete_leaves_other_keys(store_path):
    store = KeyStore(store_path)
    store.store("A", SECRET)
    store.store("B", OTHER_SECRET)

    assert store.delete("A") is True
    assert store.list_names() == ["B"]
    assert store.get("B") == OTHER_SECRET


def test_describe_returns_masked_records_only(store_path):
    store = KeyStore(store_path)
    store.store("OMNIROUTE_API_KEY", SECRET)

    described = store.describe()
    assert [record.name for record in described] == ["OMNIROUTE_API_KEY"]
    rendered = repr(described) + str(described[0])
    assert SECRET not in rendered
    assert SECRET_MID not in rendered
    assert SECRET_FINGERPRINT in rendered


# ---------------------------------------------------------------------------
# Permissions: 0600 at creation, tightening reported, never a readable window
# ---------------------------------------------------------------------------

def test_store_file_is_0600_at_creation(store_path):
    assert not store_path.exists()

    KeyStore(store_path).store("OMNIROUTE_API_KEY", SECRET)

    assert store_path.exists()
    assert _mode(store_path) == 0o600


def test_rewrite_keeps_the_file_at_0600(store_path):
    store = KeyStore(store_path)
    store.store("A", SECRET)
    store.store("B", OTHER_SECRET)
    store.delete("A")

    assert _mode(store_path) == 0o600


def test_temp_file_is_0600_from_the_moment_mkstemp_creates_it(
    store_path, monkeypatch
):
    """The write must go through an fd that is private before any byte is written.

    This is the "never create a world-readable file and chmod it afterwards"
    guarantee: the fd handed back by ``mkstemp`` is already 0600, so no window
    exists in which a credential sits in a readable file.
    """
    observed = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        observed.append((name, stat.S_IMODE(os.fstat(fd).st_mode)))
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)

    KeyStore(store_path).store("OMNIROUTE_API_KEY", SECRET)

    assert len(observed) == 1
    name, mode = observed[0]
    assert mode & 0o077 == 0, f"{name} was created group/other-readable: {oct(mode)}"


def test_temp_file_is_0600_at_the_moment_of_the_rename(store_path, monkeypatch):
    observed = []
    real_replace = os.replace

    def spy_replace(src, dst):
        observed.append((str(src), stat.S_IMODE(os.stat(src).st_mode)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    KeyStore(store_path).store("OMNIROUTE_API_KEY", SECRET)

    assert len(observed) == 1
    assert observed[0][1] == 0o600
    assert observed[0][0] != str(store_path)


def test_creation_never_uses_chmod_to_repair_a_loose_file(
    store_path, monkeypatch
):
    """No ``chmod`` may ever be the thing that makes a file private.

    Each ``os.chmod`` call is inspected with the mode the target had *before*
    the call.  A chmod that found a group/other-readable file would prove the
    store created a readable file first and repaired it afterwards.
    """
    seen = []
    real_chmod = os.chmod

    def spy_chmod(path, mode, *args, **kwargs):
        try:
            before = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            before = None
        seen.append((str(path), before, mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", spy_chmod)

    store = KeyStore(store_path)
    store.store("OMNIROUTE_API_KEY", SECRET)

    for path, before, _mode_requested in seen:
        if before is None:
            continue  # a path that did not exist yet cannot have leaked
        assert before & 0o077 == 0, (
            f"chmod repaired {path} from {oct(before)}: the file was created "
            "group/other-readable first"
        )
    assert _mode(store_path) == 0o600


def test_existing_0644_file_is_tightened_and_reported(store_path, caplog):
    """A pre-existing loose file is tightened — and the tightening is reported.

    Reported three ways, all asserted here: a WARNING log line, a
    ``PermissionReport`` on the store, and the ``on_tighten`` callback.
    """
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps({"version": 1, "keys": {"OLD_KEY": OTHER_SECRET}}, indent=2)
    )
    os.chmod(store_path, 0o644)
    assert _mode(store_path) == 0o644

    callbacks = []
    with caplog.at_level(logging.WARNING, logger="claire.platform.keys"):
        store = KeyStore(store_path, on_tighten=callbacks.append)

    assert _mode(store_path) == 0o600

    reports = store.tightened_permissions
    assert len(reports) == 1
    report = reports[0]
    assert report.previous_mode == 0o644
    assert report.new_mode == 0o600
    assert str(report) == f"tightened {store_path} from 0o644 to 0o600"
    assert callbacks == [report]

    assert "tightened" in caplog.text
    assert "0o644" in caplog.text
    assert "0o600" in caplog.text
    assert OTHER_SECRET not in caplog.text

    # The pre-existing key survived the tightening.
    assert store.get("OLD_KEY") == OTHER_SECRET


def test_tightening_is_reported_before_any_key_is_used(store_path, monkeypatch):
    """Tightening happens on the operation, not lazily some time later."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"version": 1, "keys": {}}))
    os.chmod(store_path, 0o606)
    assert _mode(store_path) == 0o606

    store = KeyStore(store_path)

    assert _mode(store_path) == 0o600
    assert len(store.tightened_permissions) == 1


def test_tightening_is_reported_only_once(store_path):
    """Once the file is private, later operations must not re-report."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"version": 1, "keys": {}}))
    os.chmod(store_path, 0o640)

    store = KeyStore(store_path)
    assert len(store.tightened_permissions) == 1

    store.store("A", SECRET)
    store.list_names()
    store.render("A")
    store.delete("A")

    assert len(store.tightened_permissions) == 1
    assert _mode(store_path) == 0o600


def test_untightenable_store_raises_named_error(store_path, monkeypatch):
    """If the mode cannot be fixed, refuse — never proceed with a loose file."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"version": 1, "keys": {}}))
    os.chmod(store_path, 0o644)

    real_chmod = os.chmod

    def deaf_chmod(path, mode, *args, **kwargs):
        if str(path) == str(store_path):
            return None  # pretend the filesystem ignored it
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", deaf_chmod)

    with pytest.raises(KeyStorePermissionError) as excinfo:
        KeyStore(store_path)

    message = str(excinfo.value)
    assert "0o644" in message
    assert f"chmod 600 {store_path}" in message
    assert OTHER_SECRET not in message


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

def test_atomic_write_leaves_no_temp_file_behind(store_path):
    store = KeyStore(store_path)
    store.store("OMNIROUTE_API_KEY", SECRET)

    leftovers = list(store_path.parent.glob("*.tmp")) + list(
        store_path.parent.glob(".*.tmp")
    )
    assert leftovers == []
    assert sorted(p.name for p in store_path.parent.iterdir()) == ["keys.json"]


def test_failed_replace_leaves_old_store_intact_and_no_temp_file(
    store_path, monkeypatch
):
    store = KeyStore(store_path)
    store.store("OMNIROUTE_API_KEY", SECRET)

    real_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("simulated crash between the write and the rename")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        store.store("OMNIROUTE_API_KEY", OTHER_SECRET)

    monkeypatch.setattr(os, "replace", real_replace)

    # The old value is still there, byte for byte, and no temp file survived.
    assert store.get("OMNIROUTE_API_KEY") == SECRET
    assert list(store_path.parent.glob("*.tmp")) == []
    assert list(store_path.parent.glob(".*.tmp")) == []
    assert json.loads(store_path.read_text())["keys"] == {"OMNIROUTE_API_KEY": SECRET}


def test_store_file_is_valid_json_with_a_version(store_path):
    KeyStore(store_path).store("OMNIROUTE_API_KEY", SECRET)

    data = json.loads(store_path.read_text())
    assert data["version"] == 1
    assert list(data["keys"]) == ["OMNIROUTE_API_KEY"]


# ---------------------------------------------------------------------------
# Masking: the value never appears anywhere a human or a file can see it
# ---------------------------------------------------------------------------

def test_mask_renders_last_four_plus_fingerprint():
    assert mask(SECRET) == f"...{SECRET_TAIL}#{SECRET_FINGERPRINT}"
    assert fingerprint(SECRET) == SECRET_FINGERPRINT
    assert len(fingerprint(SECRET)) == 8


def test_short_values_reveal_no_characters_at_all():
    """Last-4 of a short secret would be most of the secret.

    The probe strings use letters outside ``[0-9a-f]`` so that "is the value a
    substring of the rendering" is a real question and not an accident of the
    hex digest containing the same character.
    """
    for short in ("Z", "ZxQ", "ZxQwvut"):
        rendered = mask(short)
        assert short not in rendered
        assert rendered.startswith("****#")
        assert rendered.endswith(fingerprint(short))


def test_mask_fingerprint_is_stable_and_distinguishing():
    assert mask("aaaaaaaaaaaa1234") == mask("aaaaaaaaaaaa1234")
    assert mask("aaaaaaaaaaaa1234") != mask("aaaaaaaaaaaa1235")


def test_render_shows_only_last_four_and_fingerprint(store_path):
    store = KeyStore(store_path)
    store.store("OMNIROUTE_API_KEY", SECRET)

    rendered = store.render("OMNIROUTE_API_KEY")

    assert rendered == f"...{SECRET_TAIL}#{SECRET_FINGERPRINT} (store)"
    assert SECRET not in rendered
    assert SECRET_HEAD not in rendered
    assert SECRET_MID not in rendered


def test_no_surface_except_get_returns_the_value(store_path, caplog):
    """Sweep every read-only surface; the value appears in exactly one place."""
    store = KeyStore(store_path)
    record = store.store("OMNIROUTE_API_KEY", SECRET)

    surfaces = {
        "render": store.render("OMNIROUTE_API_KEY"),
        "source_of": store.source_of("OMNIROUTE_API_KEY"),
        "list_names": repr(store.list_names()),
        "describe": repr(store.describe()),
        "describe str": " ".join(str(r) for r in store.describe()),
        "repr(store)": repr(store),
        "str(store)": str(store),
        "repr(record)": repr(record),
        "str(record)": str(record),
        "len(store)": str(len(store)),
        "iter(store)": repr(list(iter(store))),
        "contains": repr("OMNIROUTE_API_KEY" in store),
        "tightened_permissions": repr(store.tightened_permissions),
    }

    for label, text in surfaces.items():
        assert SECRET not in text, f"{label} leaked the value"
        assert SECRET_MID not in text, f"{label} leaked part of the value"
        assert SECRET_HEAD not in text, f"{label} leaked the value prefix"

    # The one deliberate exception.
    assert store.get("OMNIROUTE_API_KEY") == SECRET


def test_repr_of_store_and_record_contain_no_secret(store_path):
    store = KeyStore(store_path)
    record = store.store("OMNIROUTE_API_KEY", SECRET)

    for text in (repr(store), str(store), repr(record), str(record)):
        assert SECRET not in text
        assert SECRET_MID not in text


def test_permission_report_repr_holds_no_key_material():
    report = PermissionReport("/tmp/keys.json", 0o644, 0o600)

    assert "tightened /tmp/keys.json from 0o644 to 0o600" == repr(report)
    assert SECRET not in repr(report)


def test_no_secret_reaches_the_log(store_path, caplog):
    """store/get/render/delete all log — none of them may log a value."""
    store = KeyStore(store_path)

    with caplog.at_level(logging.DEBUG):
        store.store("OMNIROUTE_API_KEY", SECRET)
        assert store.get("OMNIROUTE_API_KEY") == SECRET
        store.render("OMNIROUTE_API_KEY")
        store.delete("OMNIROUTE_API_KEY")
        with pytest.raises(KeyNotFoundError):
            store.get("OMNIROUTE_API_KEY")

    text = caplog.text
    assert text.strip(), "expected the store to log something at all"
    assert SECRET not in text
    assert SECRET_MID not in text
    assert SECRET_HEAD not in text
    # The masked rendering is what a log line is allowed to carry.
    assert SECRET_FINGERPRINT in text


def test_public_api_returns_raw_from_get_alone():
    """Structural guard: exactly one function returns a stored value.

    ``_read_file`` is the private storage layer (it hands back the whole
    mapping and is never called outside this module).  Apart from that, the
    only function whose ``return`` expression subscribes into a mapping of
    stored values is ``get``.
    """
    tree = ast.parse(inspect.getsource(keys))
    subscript_returners = set()
    mapping_returners = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Return) or inner.value is None:
                continue
            value = inner.value
            if (
                isinstance(value, ast.Subscript)
                and isinstance(value.value, ast.Name)
                and value.value.id in {"keys", "data", "stored"}
            ):
                subscript_returners.add(node.name)
            if isinstance(value, ast.Name) and value.id in {"keys", "data"}:
                mapping_returners.add(node.name)

    assert subscript_returners == {"get"}
    assert mapping_returners <= {"_read_file"}


def test_documented_raw_accessor_constant_names_get():
    assert keys.RAW_VALUE_ACCESSOR == "KeyStore.get"


# ---------------------------------------------------------------------------
# Missing keys are named errors, never silent no-ops
# ---------------------------------------------------------------------------

def test_missing_name_raises_named_error_naming_the_name(store_path):
    store = KeyStore(store_path)
    store.store("PRESENT_KEY", SECRET)

    with pytest.raises(KeyNotFoundError) as excinfo:
        store.get("ABSENT_KEY")

    error = excinfo.value
    assert error.name == "ABSENT_KEY"
    message = str(error)
    assert "ABSENT_KEY" in message
    assert str(store_path) in message
    assert "claire keys set ABSENT_KEY" in message
    # Naming the name is required; naming any value is forbidden.
    assert SECRET not in message
    assert SECRET_MID not in message


def test_missing_name_error_mentions_the_env_var_that_was_checked(store_path):
    store = KeyStore(store_path)

    with pytest.raises(KeyNotFoundError) as excinfo:
        store.get("OMNIROUTE_API_KEY")

    message = str(excinfo.value)
    assert "OMNIROUTE_API_KEY" in message
    assert "export OMNIROUTE_API_KEY=<your key>" in message


def test_missing_name_error_from_render_and_source_of(store_path):
    store = KeyStore(store_path)

    with pytest.raises(KeyNotFoundError):
        store.render("ABSENT_KEY")
    with pytest.raises(KeyNotFoundError):
        store.source_of("ABSENT_KEY")


def test_empty_value_is_a_named_error_not_a_silent_store(store_path):
    store = KeyStore(store_path)

    for empty in ("", "   ", "\n"):
        with pytest.raises(KeyValueError) as excinfo:
            store.store("SOME_KEY", empty)
        assert "SOME_KEY" in str(excinfo.value)

    assert store.list_names() == []
    assert not store_path.exists()


def test_non_string_value_is_a_named_error(store_path):
    store = KeyStore(store_path)

    with pytest.raises(KeyValueError) as excinfo:
        store.store("SOME_KEY", 12345)

    assert "SOME_KEY" in str(excinfo.value)
    assert "must be a string" in str(excinfo.value)


def test_value_with_embedded_newline_is_a_named_error(store_path):
    store = KeyStore(store_path)

    with pytest.raises(KeyValueError):
        store.store("SOME_KEY", f"line-one\n{SECRET}")


def test_invalid_names_are_named_errors(store_path):
    store = KeyStore(store_path)

    for bad in ("", "   ", "has space", "../escape", "a" * 200):
        with pytest.raises(KeyNameError):
            store.store(bad, SECRET)
    with pytest.raises(KeyNameError):
        store.get(42)


def test_all_store_errors_share_a_base_class():
    for error in (
        KeyNameError,
        KeyValueError,
        KeyStoreCorruptError,
        KeyStorePermissionError,
        KeyNotFoundError,
    ):
        assert issubclass(error, KeyStoreError)


# ---------------------------------------------------------------------------
# Corrupt file: report the problem, never quote the document
# ---------------------------------------------------------------------------

def test_corrupt_json_error_does_not_quote_the_file(store_path):
    store_path.parent.mkdir(parents=True, exist_ok=True)
    # Malformed JSON that still contains a credential-shaped string: any error
    # message derived from json.JSONDecodeError would quote it back.
    store_path.write_text('{"version": 1, "keys": {"K": "' + SECRET + '"')
    os.chmod(store_path, 0o600)

    store = KeyStore(store_path)

    with pytest.raises(KeyStoreCorruptError) as excinfo:
        store.list_names()

    message = str(excinfo.value)
    assert str(store_path) in message
    assert "not valid JSON" in message
    assert SECRET not in message
    assert SECRET_MID not in message
    assert SECRET_HEAD not in message
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


def test_unexpected_shape_error_does_not_quote_the_file(store_path):
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"credentials": {"K": SECRET}}))
    os.chmod(store_path, 0o600)

    with pytest.raises(KeyStoreCorruptError) as excinfo:
        KeyStore(store_path).list_names()

    assert "unexpected shape" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)


def test_non_utf8_store_error_names_the_path(store_path):
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"\xff\xfe\x00\x01")
    os.chmod(store_path, 0o600)

    with pytest.raises(KeyStoreCorruptError) as excinfo:
        KeyStore(store_path).list_names()

    assert str(store_path) in str(excinfo.value)
    assert "UTF-8" in str(excinfo.value)


def test_empty_file_reads_as_an_empty_store(store_path):
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text("")
    os.chmod(store_path, 0o600)

    assert KeyStore(store_path).list_names() == []


# ---------------------------------------------------------------------------
# Optional environment fallback
# ---------------------------------------------------------------------------

def test_env_fallback_is_used_and_the_source_is_reported(store_path, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "env-omniroute-abcdef-5678")
    store = KeyStore(store_path)

    assert store.get("OMNIROUTE_API_KEY") == "env-omniroute-abcdef-5678"
    assert store.source_of("OMNIROUTE_API_KEY") == "env:OMNIROUTE_API_KEY"
    rendered = store.render("OMNIROUTE_API_KEY")
    assert "env:OMNIROUTE_API_KEY" in rendered
    assert "env-omniroute" not in rendered
    assert rendered.startswith("...5678#")
    # Nothing was written to disk just because it was read from the env.
    assert store.list_names() == []


def test_store_wins_over_the_environment(store_path, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "env-omniroute-abcdef-5678")
    store = KeyStore(store_path)
    store.store("OMNIROUTE_API_KEY", SECRET)

    assert store.source_of("OMNIROUTE_API_KEY") == "store"
    assert store.get("OMNIROUTE_API_KEY") == SECRET
    assert store.render("OMNIROUTE_API_KEY").endswith("(store)")


def test_blank_env_var_is_not_a_source(store_path, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "   ")
    store = KeyStore(store_path)

    with pytest.raises(KeyNotFoundError):
        store.get("OMNIROUTE_API_KEY")


def test_explicit_env_var_argument_overrides_the_default_table(store_path, monkeypatch):
    monkeypatch.setenv("SOME_OTHER_KEY", "other-abcdefghij-4321")
    store = KeyStore(store_path)

    assert store.get("NOT_IN_TABLE", env_var="SOME_OTHER_KEY") == "other-abcdefghij-4321"
    assert store.source_of("NOT_IN_TABLE", env_var="SOME_OTHER_KEY") == "env:SOME_OTHER_KEY"


def test_env_mapping_can_be_injected(store_path):
    store = KeyStore(store_path, env={"OMNIROUTE_API_KEY": "injected-value-0000"})

    assert store.get("OMNIROUTE_API_KEY") == "injected-value-0000"
    assert store.source_of("OMNIROUTE_API_KEY") == "env:OMNIROUTE_API_KEY"


# ---------------------------------------------------------------------------
# Path resolution: the store is never inside the repository
# ---------------------------------------------------------------------------

def test_default_store_path_follows_xdg(cfg_home):
    assert default_store_path() == cfg_home / "claire" / "keys.json"


def test_default_store_path_can_be_overridden(cfg_home, monkeypatch, tmp_path):
    override = tmp_path / "elsewhere" / "store.json"
    monkeypatch.setenv("CLAIRE_KEY_STORE", str(override))

    assert default_store_path() == override


def test_default_store_path_is_outside_the_repository(cfg_home):
    resolved = default_store_path().resolve()

    assert REPO_ROOT.resolve() not in resolved.parents
    assert ".git" not in resolved.parts
    assert resolved.name == "keys.json"


def test_creating_the_default_store_uses_the_redirected_config_dir(cfg_home):
    store = KeyStore()
    store.store("OMNIROUTE_API_KEY", SECRET)

    assert store.path == cfg_home / "claire" / "keys.json"
    assert _mode(store.path) == 0o600
    assert REPO_ROOT.resolve() not in store.path.resolve().parents


def test_created_config_dir_is_private(cfg_home):
    store = KeyStore()

    assert _mode(store.path.parent) == 0o700


def test_loose_config_dir_is_noted_but_left_alone(store_path, caplog):
    """A directory Claire shares with other tools is never silently chmod-ed.

    The note makes the situation visible; the store file inside stays 0600,
    which is what actually protects the key.
    """
    store_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(store_path.parent, 0o755)

    with caplog.at_level(logging.INFO, logger="claire.platform.keys"):
        store = KeyStore(store_path)

    assert _mode(store_path.parent) == 0o755  # the user's directory: untouched
    assert "key store directory" in caplog.text
    assert "0o755" in caplog.text

    store.store("OMNIROUTE_API_KEY", SECRET)
    assert _mode(store_path) == 0o600
    assert SECRET not in caplog.text
