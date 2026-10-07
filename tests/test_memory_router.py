"""Unit tests for :mod:`claire.memory.router` — the always-loaded memory file.

Every test works in ``tmp_path`` and passes an explicit path, so the real vault
is never read or written.

The focus is behaviour the design promises:

* frontmatter is parsed, and absent/malformed frontmatter falls back to defaults;
* ``render_for_prompt()`` re-reads the file from disk on every call (an edit
  takes effect without a restart);
* the body is hard-capped: past the budget it is truncated at a *line boundary*
  and a loud warning names the real size, the budget and the dropped bytes;
* a missing file is created from the template and that creation is reported;
* an unreadable or undecodable file raises a named error, never ``''``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from claire.memory import notes
from claire.memory import router as router_module
from claire.memory.notes import ROUTER_BUDGET_BYTES, RouterNotFoundError
from claire.memory.router import (
    MemoryRouter,
    RouterDecodeError,
    RouterReadError,
    ensure_router,
    load_router,
    router_template,
)

HEADER_MARKER = "always-loaded memory"

@pytest.fixture(autouse=True)
def _never_touch_the_real_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the vault env var, so no test can read or write real memory."""
    monkeypatch.setenv(notes.VAULT_ENV_VAR, str(tmp_path / "unused-real-vault"))
    assert notes.resolve_vault_dir() == tmp_path / "unused-real-vault"

def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path

def _router_path(tmp_path: Path) -> Path:
    return tmp_path / "vault" / "CLAIRE.md"

def _body_from_lines(lines) -> str:
    return "".join(line + "\n" for line in lines)

# --------------------------------------------------------------------------
# Frontmatter
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_frontmatter_is_parsed_and_path_is_kept(tmp_path: Path) -> None:
    path = _write(
        _router_path(tmp_path),
        "---\ntitle: Claire memory router\nrole: router\n"
        "always_loaded: true\nbudget_bytes: 4500\nupdated: 2026-01-31\n---\n"
        "\n# Pointers\n\n- dentist: notes/dentist.md - Wednesdays 16:00.\n",
    )

    router = load_router(path)

    assert isinstance(router, MemoryRouter)
    assert router.path == path
    assert router.frontmatter["title"] == "Claire memory router"
    assert router.frontmatter["role"] == "router"
    assert router.frontmatter["always_loaded"] == "true"
    assert router.frontmatter["updated"] == "2026-01-31"
    assert router.budget_bytes == 4500
    assert "notes/dentist.md" in router.body
    assert "---" not in router.body.splitlines()[0]

@pytest.mark.unit
def test_absent_frontmatter_falls_back_to_defaults(tmp_path: Path) -> None:
    path = _write(_router_path(tmp_path), "# Pointers\n\n- dentist: notes/dentist.md\n")

    router = load_router(path)

    assert router.frontmatter == {}
    assert router.budget_bytes == ROUTER_BUDGET_BYTES == 6000
    assert "notes/dentist.md" in router.body

@pytest.mark.unit
def test_unterminated_frontmatter_is_tolerated_as_body(tmp_path: Path) -> None:
    text = "---\ntitle: half written\nrole: router\n\n# Pointers\n"
    path = _write(_router_path(tmp_path), text)

    router = load_router(path)

    assert router.frontmatter == {}
    assert router.budget_bytes == ROUTER_BUDGET_BYTES
    # Nothing was lost: the unterminated block stayed in the body.
    assert router.body == text

@pytest.mark.unit
def test_malformed_frontmatter_lines_are_skipped_not_fatal(tmp_path: Path) -> None:
    path = _write(
        _router_path(tmp_path),
        "---\nthis line has no colon\nbudget_bytes: not-a-number\n---\n\n- one fact\n",
    )

    router = load_router(path)

    assert "this line has no colon" not in router.frontmatter
    assert router.budget_bytes == ROUTER_BUDGET_BYTES
    assert "one fact" in router.body

@pytest.mark.unit
def test_non_positive_budget_override_is_ignored(tmp_path: Path) -> None:
    path = _write(
        _router_path(tmp_path),
        "---\nbudget_bytes: 0\n---\n\n- one fact\n",
    )

    router = load_router(path)

    assert router.budget_bytes == ROUTER_BUDGET_BYTES

# --------------------------------------------------------------------------
# Under budget
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_under_budget_body_is_returned_intact_without_warning(tmp_path: Path) -> None:
    body = _body_from_lines([f"- fact {i}: something short." for i in range(10)])
    path = _write(_router_path(tmp_path), "---\ntitle: t\nrole: router\n---\n\n" + body)

    router = load_router(path)
    rendered = router.render_for_prompt()

    assert router.over_budget is False
    assert router.dropped_bytes == 0
    assert router.size_bytes == len(router.body.encode("utf-8"))
    assert HEADER_MARKER in rendered
    for line in body.splitlines():
        assert line in rendered
    assert "WARNING" not in rendered
    assert "TRUNCATED" not in rendered
    assert "OVER BUDGET" not in rendered

@pytest.mark.unit
def test_header_says_this_memory_is_always_loaded_and_owner_editable(
    tmp_path: Path,
) -> None:
    path = _write(_router_path(tmp_path), "---\nrole: router\n---\n\n- one fact\n")

    rendered = load_router(path).render_for_prompt()

    assert HEADER_MARKER in rendered
    assert "edit" in rendered
    assert "no restart" in rendered
    assert "notes/" in rendered

# --------------------------------------------------------------------------
# Over budget: truncate at a line boundary, warn loudly
# --------------------------------------------------------------------------

def _over_budget_lines(count: int = 30, width: int = 40) -> list:
    """Distinct lines, each unique, so a half-cut line is detectable."""
    return [f"fact-{i:02d}: " + "y" * width for i in range(count)]

def _truncation_point(body: str, budget: int) -> int:
    """Number of leading lines of *body* that fit inside *budget* bytes."""
    used = 0
    lines = body.splitlines(keepends=True)
    for index, line in enumerate(lines):
        width = len(line.encode("utf-8"))
        if used + width > budget:
            return index
        used += width
    return len(lines)

@pytest.mark.unit
def test_over_budget_truncates_at_line_boundary_and_warns(tmp_path: Path) -> None:
    budget = 400
    lines = _over_budget_lines()
    path = _write(
        _router_path(tmp_path),
        f"---\ntitle: t\nrole: router\nbudget_bytes: {budget}\n---\n\n"
        + _body_from_lines(lines),
    )
    router = load_router(path)

    assert router.over_budget is True
    assert router.budget_bytes == budget

    body_lines = router.body.splitlines(keepends=True)
    cut = _truncation_point(router.body, budget)
    kept_text = "".join(body_lines[:cut])
    first_dropped = body_lines[cut]
    assert cut > 1  # some content is kept, and the first fact fits
    assert cut < len(body_lines)

    rendered = router.render_for_prompt()

    # The warning is loud and names all three real numbers.
    assert "WARNING" in rendered
    assert "OVER BUDGET" in rendered
    assert str(router.size_bytes) in rendered
    assert str(budget) in rendered
    assert str(router.dropped_bytes) in rendered
    assert router.dropped_bytes > 0
    # The warning comes first: the model sees the problem before the content.
    assert rendered.index("WARNING") < rendered.index(body_lines[1])

    # Nothing is half-cut: every kept line is whole, and the first dropped fact
    # is entirely absent - not even its opening characters leaked in.
    for line in body_lines[:cut]:
        if line.strip():
            assert line in rendered
    assert first_dropped not in rendered
    assert first_dropped[:20] not in rendered

    # Byte accounting is honest: kept + dropped == real size.
    assert len(kept_text.encode("utf-8")) == router.size_bytes - router.dropped_bytes
    assert len(kept_text.encode("utf-8")) <= budget
    dropped_tail = "".join(body_lines[cut:])
    assert router.dropped_bytes == len(dropped_tail.encode("utf-8"))

@pytest.mark.unit
def test_over_budget_uses_default_6000_byte_budget_without_frontmatter(
    tmp_path: Path,
) -> None:
    lines = [f"line-{i:03d}: " + "z" * 80 for i in range(100)]
    path = _write(_router_path(tmp_path), _body_from_lines(lines))
    router = load_router(path)

    assert router.size_bytes > 6000
    rendered = router.render_for_prompt()

    assert router.over_budget is True
    assert router.budget_bytes == ROUTER_BUDGET_BYTES == 6000
    assert "6000" in rendered
    assert str(router.size_bytes) in rendered
    assert str(router.dropped_bytes) in rendered

@pytest.mark.unit
def test_single_line_bigger_than_budget_is_dropped_whole_not_cut(
    tmp_path: Path,
) -> None:
    budget = 200
    giant = "giant-fact: " + "q" * 700
    path = _write(
        _router_path(tmp_path),
        f"---\nbudget_bytes: {budget}\n---\n\n{giant}\n",
    )
    router = load_router(path)

    rendered = router.render_for_prompt()

    assert router.over_budget is True
    # Only the blank separator line fits; the over-long line is dropped whole.
    assert router.dropped_bytes == len(giant.encode("utf-8")) + 1
    assert "WARNING" in rendered
    assert "giant-fact" not in rendered
    assert giant[:40] not in rendered

@pytest.mark.unit
def test_render_does_not_mutate_the_body_it_reports_on(tmp_path: Path) -> None:
    budget = 300
    lines = _over_budget_lines()
    path = _write(
        _router_path(tmp_path),
        f"---\nbudget_bytes: {budget}\n---\n\n" + _body_from_lines(lines),
    )
    router = load_router(path)

    router.render_for_prompt()

    # body/size still describe the file, so the warning stays true on re-render.
    assert router.body.lstrip("\n") == _body_from_lines(lines)
    assert router.over_budget is True
    assert router.render_for_prompt().count("WARNING") == 1

# --------------------------------------------------------------------------
# Edit without restart: no caching, ever
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_edit_without_restart_is_visible_on_the_same_object(tmp_path: Path) -> None:
    path = _write(
        _router_path(tmp_path),
        "---\ntitle: t\nrole: router\nbudget_bytes: 6000\n---\n\n- coffee: old brand\n",
    )
    router = load_router(path)
    first = router.render_for_prompt()
    assert "old brand" in first

    # The owner edits the file by hand, mid-session.
    _write(
        path,
        "---\ntitle: t\nrole: router\nbudget_bytes: 5000\n---\n\n- coffee: new brand\n",
    )

    second = router.render_for_prompt()

    assert "new brand" in second
    assert "old brand" not in second
    assert "new brand" in router.body
    assert router.budget_bytes == 5000

# --------------------------------------------------------------------------
# Creating a missing file: reported, idempotent, never silent
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_ensure_router_creates_from_template_reports_and_is_idempotent(
    tmp_path: Path,
) -> None:
    path = _router_path(tmp_path)  # parent directory does not exist either

    first = ensure_router(path)

    assert path.exists()
    assert first.created_from_template is True
    assert first.frontmatter["role"] == "router"
    assert first.frontmatter["always_loaded"] == "true"
    assert first.budget_bytes == ROUTER_BUDGET_BYTES
    assert first.frontmatter["title"]
    assert first.frontmatter["updated"]
    assert "notes/" in first.body
    assert first.size_bytes < ROUTER_BUDGET_BYTES  # a router, not a database
    assert first.over_budget is False
    assert "WARNING" not in first.render_for_prompt()

    raw_after_first = path.read_bytes()

    second = ensure_router(path)

    assert second.created_from_template is False
    assert path.read_bytes() == raw_after_first

    # An owner-edited router is never overwritten.
    _write(path, "---\ntitle: mine\nrole: router\n---\n\n- keep me exactly\n")
    third = ensure_router(path)

    assert third.created_from_template is False
    assert "keep me exactly" in third.body

@pytest.mark.unit
def test_creation_from_template_is_reported_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _router_path(tmp_path)

    with caplog.at_level(logging.INFO, logger="claire.memory.router"):
        ensure_router(path)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "template" in message.lower() and str(path) in message for message in messages
    ), messages
    assert all(record.levelno >= logging.INFO for record in caplog.records)

@pytest.mark.unit
def test_load_router_creates_a_missing_file_by_default(tmp_path: Path) -> None:
    path = _router_path(tmp_path)

    router = load_router(path)

    assert path.exists()
    assert router.created_from_template is True

@pytest.mark.unit
def test_load_router_with_create_false_names_the_missing_file(tmp_path: Path) -> None:
    path = _router_path(tmp_path)

    with pytest.raises(RouterNotFoundError) as excinfo:
        load_router(path, create=False)

    assert str(path) in str(excinfo.value)
    assert not path.exists()

@pytest.mark.unit
def test_router_template_is_a_usable_router_well_under_budget() -> None:
    template = router_template(updated="2026-01-31")
    meta, body = notes.parse_frontmatter(template)

    assert meta["role"] == "router"
    assert meta["always_loaded"] == "true"
    assert meta["budget_bytes"] == str(ROUTER_BUDGET_BYTES)
    assert meta["updated"] == "2026-01-31"
    assert "notes/" in body
    assert len(template.encode("utf-8")) < ROUTER_BUDGET_BYTES // 4

# --------------------------------------------------------------------------
# Failures are named, never silently empty
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_unreadable_path_raises_named_error(tmp_path: Path) -> None:
    path = _router_path(tmp_path)
    path.mkdir(parents=True)  # a directory where the router file should be

    with pytest.raises(RouterReadError) as excinfo:
        load_router(path)

    assert str(path) in str(excinfo.value)
    assert isinstance(excinfo.value, OSError)  # friendly to `except OSError`

@pytest.mark.unit
@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores file permission bits"
)
def test_permission_denied_raises_named_error(tmp_path: Path) -> None:
    path = _write(_router_path(tmp_path), "---\nrole: router\n---\n\n- one fact\n")
    path.chmod(0o000)
    try:
        with pytest.raises(RouterReadError) as excinfo:
            load_router(path)
        assert str(path) in str(excinfo.value)
    finally:
        path.chmod(0o600)

@pytest.mark.unit
def test_undecodable_file_raises_named_error(tmp_path: Path) -> None:
    path = _router_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"---\nrole: router\n---\n\n\xff\xfe\x00 not utf-8\n")

    with pytest.raises(RouterDecodeError) as excinfo:
        load_router(path)

    assert str(path) in str(excinfo.value)
    assert "UTF-8" in str(excinfo.value)
    assert isinstance(excinfo.value, RouterReadError)

@pytest.mark.unit
def test_render_after_the_file_vanishes_raises_named_error(tmp_path: Path) -> None:
    path = _write(_router_path(tmp_path), "---\nrole: router\n---\n\n- one fact\n")
    router = load_router(path)

    path.unlink()

    with pytest.raises(RouterNotFoundError):
        router.render_for_prompt()

@pytest.mark.unit
def test_named_errors_are_router_errors() -> None:
    assert issubclass(RouterReadError, router_module.RouterError)
    assert issubclass(RouterDecodeError, RouterReadError)
    assert issubclass(router_module.RouterWriteError, router_module.RouterError)
    assert issubclass(router_module.RouterError, notes.NotesError)
