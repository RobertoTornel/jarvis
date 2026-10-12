"""The small-talk router skip is a configuration knob, and it is pinned.

Evidence for the default: 33 of the 39 tool-router calls in a fortnight of the
owner's journal selected no tools at all, and the router is a chat-model call
at roughly 1.3 s. The prefilter that skips it must be overridable, because it
is a routing decision: a false positive costs a tool call, and the owner needs
a way out without editing code.
"""

import dataclasses
import json

import pytest

from claire.config import Settings, get_default_config, load_settings

pytestmark = pytest.mark.unit


def _write_config(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
    return cfg_path


def test_the_skip_is_on_by_default():
    """The measured cost of not skipping is 85% of router calls wasted."""
    assert get_default_config()["smalltalk_router_skip_enabled"] is True


def test_settings_declares_the_field():
    named = {f.name for f in dataclasses.fields(Settings)}

    assert "smalltalk_router_skip_enabled" in named


def test_the_skip_can_be_turned_off(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"smalltalk_router_skip_enabled": False})

    assert load_settings().smalltalk_router_skip_enabled is False


def test_a_quoted_false_is_false(tmp_path, monkeypatch):
    """bool("false") is True, which would silently ignore the user's choice."""
    _write_config(tmp_path, monkeypatch, {"smalltalk_router_skip_enabled": "false"})

    assert load_settings().smalltalk_router_skip_enabled is False


def test_a_quoted_true_is_true(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"smalltalk_router_skip_enabled": "true"})

    assert load_settings().smalltalk_router_skip_enabled is True


def test_it_can_be_turned_on_from_the_default_config(tmp_path, monkeypatch):
    """Absent key keeps the default rather than flipping to False."""
    _write_config(tmp_path, monkeypatch, {})

    assert load_settings().smalltalk_router_skip_enabled is True
