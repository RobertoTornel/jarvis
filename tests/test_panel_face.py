"""Unit tests for the panel's face: ``GET /face/state`` and the index card.

The face mirrors the daemon's state file —
``$XDG_RUNTIME_DIR/claire/claire_state``, with the old temp-dir path as a
fallback, because ``PrivateTmp=yes`` on the units makes ``/tmp`` private to
each service (see ``docs/PANEL_FACE.md``).  These tests point the channel at
scratch paths, so nothing here touches the real files.  The fail-proofs:

* **The endpoint follows the file.**  A state written between requests is the
  state served; there is no caching and no snapshot taken at import time.
* **The read is total.**  Missing, empty, unreadable and unrecognised files all
  answer ``asleep`` with ``ok: false`` — never a busier guess, never a raise.
* **Path resolution is explicit.**  The shared runtime path wins when it
  exists; the temp-dir fallback is used when ``XDG_RUNTIME_DIR`` is absent;
  a garbage shared file is ``asleep`` rather than a stale fallback value.
* **The page degrades.**  The index server-renders the current state (so the
  face is correct with JavaScript disabled), carries exactly the panel's own
  poll script, and every animation declaration is gated behind
  ``prefers-reduced-motion: no-preference`` while the five shapes stay
  distinguishable without it.

Nothing here starts a server: every request is a :class:`PanelRequest` handed
to :func:`dispatch`.  The gate is ``pytest tests/ -m unit`` and it SILENTLY
DESELECTS a file with no unit marker — hence ``pytestmark = pytest.mark.unit``.
"""

import json
from pathlib import Path

import pytest

from claire.panel import server as panel
from claire.panel.server import (
    FACE_STATES,
    GET_ROUTES,
    ROUTE_FACE_STATE,
    PanelRequest,
    dispatch,
    render_index,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

@pytest.fixture()
def state_root(tmp_path, monkeypatch):
    """Point the face channel at a scratch runtime dir.

    Returns the directory that stands in for ``$XDG_RUNTIME_DIR``; the shared
    state file lives at ``<root>/claire/claire_state`` and the legacy fallback
    at ``<root>/legacy/claire_state``.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(panel, "FACE_STATE_FILE", str(tmp_path / "legacy" / "claire_state"))
    return tmp_path


def set_state(path: Path, value: str) -> None:
    """Write the state the way the daemon does: the bare name, no newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def get_face_state() -> dict:
    response = dispatch(object(), PanelRequest(method="GET", path=ROUTE_FACE_STATE))
    assert response.status == 200
    return json.loads(response.text)


# --------------------------------------------------------------------------- #
# The endpoint follows the file
# --------------------------------------------------------------------------- #

def test_face_state_route_is_declared_and_dispatched():
    """A route advertised in GET_ROUTES is wired into GET_VIEWS and dispatch."""
    assert ROUTE_FACE_STATE in GET_ROUTES
    assert panel.GET_VIEWS[ROUTE_FACE_STATE] is panel.render_face_state


def test_face_state_follows_the_shared_file(state_root):
    shared = state_root / "claire" / "claire_state"
    set_state(shared, "listening")
    assert get_face_state() == {"state": "listening", "ok": True}
    set_state(shared, "thinking")
    assert get_face_state() == {"state": "thinking", "ok": True}


def test_face_state_accepts_exactly_the_five_names(state_root):
    shared = state_root / "claire" / "claire_state"
    for name in FACE_STATES:
        set_state(shared, name)
        assert get_face_state()["state"] == name


def test_face_state_tolerates_a_trailing_newline(state_root):
    set_state(state_root / "claire" / "claire_state", "idle\n")
    assert get_face_state()["state"] == "idle"


def test_face_state_is_json_with_the_read_only_headers(state_root):
    set_state(state_root / "claire" / "claire_state", "idle")
    response = dispatch(object(), PanelRequest(method="GET", path=ROUTE_FACE_STATE))
    assert response.content_type == "application/json; charset=utf-8"
    headers = dict(response.headers)
    assert headers["X-Content-Type-Options"] == "nosniff"
    # The poll runs twice a second; a cached answer would be a stale face.
    assert headers["Cache-Control"] == "no-store"


def test_a_post_to_the_face_route_is_a_405(state_root):
    """A read-only route refuses a write by name, like every other GET route."""
    response = dispatch(
        object(), PanelRequest(method="POST", path=ROUTE_FACE_STATE, body=b"state=idle")
    )
    assert response.status == 405
    assert "face" in response.text.lower()


# --------------------------------------------------------------------------- #
# Path resolution: shared runtime dir first, temp-dir fallback second
# --------------------------------------------------------------------------- #

def test_shared_path_is_tried_before_the_legacy_path(state_root):
    shared, legacy = (
        state_root / "claire" / "claire_state",
        state_root / "legacy" / "claire_state",
    )
    assert panel._face_state_paths() == (str(shared), str(legacy))


def test_without_a_runtime_dir_only_the_legacy_path_remains(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(panel, "FACE_STATE_FILE", str(tmp_path / "legacy" / "claire_state"))
    assert panel._face_state_paths() == (str(tmp_path / "legacy" / "claire_state"),)


def test_the_shared_file_wins_over_a_stale_legacy_file(state_root):
    set_state(state_root / "claire" / "claire_state", "speaking")
    set_state(state_root / "legacy" / "claire_state", "thinking")
    assert get_face_state()["state"] == "speaking"


def test_the_legacy_path_is_used_when_the_runtime_dir_is_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    legacy = tmp_path / "legacy" / "claire_state"
    monkeypatch.setattr(panel, "FACE_STATE_FILE", str(legacy))
    set_state(legacy, "listening")
    assert get_face_state()["state"] == "listening"


def test_a_garbage_shared_file_is_asleep_not_a_stale_legacy_value(state_root):
    """A newer writer's file is authoritative even when it holds nothing usable."""
    set_state(state_root / "claire" / "claire_state", "banana")
    set_state(state_root / "legacy" / "claire_state", "idle")
    body = get_face_state()
    assert body["state"] == "asleep"
    assert body["ok"] is False


# --------------------------------------------------------------------------- #
# The read is total: anything unusable is asleep, and nothing raises
# --------------------------------------------------------------------------- #

def test_a_missing_file_is_asleep(state_root):
    assert not (state_root / "claire" / "claire_state").exists()
    body = get_face_state()
    assert body["state"] == "asleep"
    assert body["ok"] is False
    assert "no usable state file" in body["detail"]


def test_an_empty_file_is_asleep(state_root):
    set_state(state_root / "claire" / "claire_state", "")
    body = get_face_state()
    assert body["state"] == "asleep"
    assert body["ok"] is False


def test_an_unrecognised_file_is_asleep(state_root):
    set_state(state_root / "claire" / "claire_state", "dancing")
    assert get_face_state()["state"] == "asleep"


def test_a_torn_write_is_asleep(state_root):
    """Half a name must never read as the whole one."""
    set_state(state_root / "claire" / "claire_state", "listenin")
    assert get_face_state()["state"] == "asleep"


def test_an_unreadable_file_is_asleep(state_root):
    """A directory where the file should be raises OSError: answered, not raised."""
    (state_root / "claire" / "claire_state").mkdir(parents=True)
    body = get_face_state()
    assert body["state"] == "asleep"
    assert body["ok"] is False


# --------------------------------------------------------------------------- #
# The index page renders the face server-side
# --------------------------------------------------------------------------- #

def _index_html() -> str:
    return render_index(object()).text


def test_the_index_server_renders_the_current_state(state_root):
    set_state(state_root / "claire" / "claire_state", "listening")
    html = _index_html()
    assert 'data-state="listening"' in html
    assert 'aria-label="Claire is listening"' in html
    assert "Claire is listening." in html


def test_the_face_card_names_the_state_in_words(state_root):
    """An accessible name plus visible words, so colour is never the only cue."""
    set_state(state_root / "claire" / "claire_state", "thinking")
    html = _index_html()
    assert 'role="img"' in html
    assert 'id="face-pill"' in html
    assert 'id="face-word"' in html


def test_the_poll_script_targets_the_state_route(state_root):
    html = _index_html()
    assert html.count("<script>") == 1
    assert 'fetch("/face/state"' in html
    assert "setInterval(poll, 500)" in html


def test_every_animation_is_gated_behind_reduced_motion(state_root):
    """With motion allowed, the face animates; with it reduced, it must not.

    The stylesheet is one string, so this counts braces from the
    ``prefers-reduced-motion: no-preference`` marker and proves that every
    ``animation:`` declaration for the face sits inside such a block, while the
    shape rules that distinguish the states sit outside them.
    """
    css = panel._CSS
    marker = "@media (prefers-reduced-motion: no-preference)"
    starts = [i for i in range(len(css)) if css.startswith(marker, i)]
    assert starts, "the reduced-motion gate has been removed"
    gated = []
    for start in starts:
        depth = 0
        for i in range(start, len(css)):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    gated.append((start, i))
                    break
    animations = [i for i in range(len(css)) if css.startswith("animation: face-", i)]
    assert animations, "the face has lost its motion"
    for i in animations:
        assert any(start < i < end for start, end in gated), (
            "an animation declaration escaped the reduced-motion gate"
        )


def test_the_five_states_have_distinct_shapes_outside_the_motion_gate(state_root):
    """Shape rules stay unconditional, so reduce still leaves five different faces.

    The stylesheet is one string; this strips every ``@media`` block out of the
    face's slice of it (brace-counted) and requires the shape selectors that
    distinguish the states to survive in the always-on remainder.
    """
    css = panel._CSS
    face_block = css[css.index("/* ---- face"):]
    always_on = list(face_block)
    i = 0
    while i < len(face_block):
        if face_block.startswith("@media", i):
            depth = 0
            start = i
            while i < len(face_block):
                if face_block[i] == "{":
                    depth += 1
                elif face_block[i] == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            for j in range(start, i):
                always_on[j] = " "
            continue
        i += 1
    always_on = "".join(always_on)
    for selector in (
        '.face-stage[data-state="asleep"] .face-eyes-closed',
        '.face-stage[data-state="idle"] .face-eyes-open',
        '.face-stage[data-state="listening"] .face-halo',
        '.face-stage[data-state="thinking"] .face-pupil',
        '.face-stage[data-state="speaking"] .face-mouth-talk',
    ):
        assert selector in always_on, f"{selector} is not in the always-on CSS"
    # And the accent appears in the face exactly once, as the listening halo:
    # one accent, and never as the sole cue for a state.
    assert always_on.count("var(--accent)") == 1
    assert ".face-halo { stroke: var(--accent);" in always_on
