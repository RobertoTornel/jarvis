"""Reply-path LLM ceilings must bound the worst case, not the typical case.

These tests pin the *fail-fast* property of the reply path rather than a
specific number. A voice reply runs a chain of serial LLM round-trips
(tool router -> planner -> memory extractor -> 1..8 chat turns -> final
synthesis). Every one of those reads its ceiling from these settings, and
before this change the ceilings were ``180 s`` (chat), ``300 s`` (tool
router and memory extractor) and ``60 s`` (embedding). A single stalled
dependency therefore held a reply open for minutes, and the memory
extractor retries once, so one hung extractor could cost ten minutes.

The reply engine's own call sites already named the intended figures as
``getattr`` fallbacks (``45.0`` for chat, ``8.0``/``10.0`` for the tool
router and extractor), but because the settings always exist those
fallbacks were dead code — the ``getattr`` never fired. These tests keep
the settings in the range the call sites were written against.
"""

import json

import pytest

from claire.config import get_default_config, load_settings

pytestmark = pytest.mark.unit


#: A reply is a serial chain, so every ceiling multiplies the user-visible
#: worst case. Nothing on this path is a long-running batch job: the most
#: expensive round-trip measured on this machine is a chat turn at ~4 s.
_MAX_REASONABLE_CEILING_SEC = 60.0

#: The original values that made a stalled dependency look like a hang.
_REGRESSED_CEILINGS = {
    "llm_chat_timeout_sec": 180.0,
    "llm_tools_timeout_sec": 300.0,
    "llm_embedding_timeout_sec": 60.0,
}


def _write_config(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values))
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
    return cfg_path


def test_default_ceilings_bound_a_stalled_reply():
    """No reply-path ceiling may exceed one minute by default."""
    defaults = get_default_config()
    offenders = {
        key: defaults.get(key)
        for key in _REGRESSED_CEILINGS
        if float(defaults.get(key, 0.0)) > _MAX_REASONABLE_CEILING_SEC
    }
    assert not offenders, (
        "reply-path LLM ceilings must fail fast; these exceed "
        f"{_MAX_REASONABLE_CEILING_SEC:g}s: {offenders}"
    )


def test_settings_do_not_reintroduce_the_multi_minute_ceilings():
    """``load_settings`` (what the daemon runs on) applies the same ceilings."""
    cfg = load_settings()
    for key, regressed in _REGRESSED_CEILINGS.items():
        value = float(getattr(cfg, key))
        assert value <= _MAX_REASONABLE_CEILING_SEC, (
            f"{key}={value} exceeds {_MAX_REASONABLE_CEILING_SEC:g}s"
        )
        assert value < regressed, f"{key} regressed back to {regressed}"


def test_a_user_config_still_overrides_the_ceiling(tmp_path, monkeypatch):
    """These are defaults, not hard limits: an explicit value must win.

    Somebody running a genuinely slow local model needs to be able to
    raise the ceiling, so the fail-fast default must stay overridable.
    """
    _write_config(
        tmp_path,
        monkeypatch,
        {"llm_chat_timeout_sec": 240.0, "llm_tools_timeout_sec": 120.0},
    )

    cfg = load_settings()

    assert cfg.llm_chat_timeout_sec == 240.0
    assert cfg.llm_tools_timeout_sec == 120.0


def test_memory_extractor_ceiling_is_the_bounded_one():
    """The extractor is the worst offender: an unbounded call plus a retry.

    ``extract_search_params_for_memory`` makes up to two attempts, and the
    engine passes ``llm_tools_timeout_sec`` to both. At the old 300 s that
    is a 600 s worst case for one background classification call, on a
    code path that cannot answer the user by itself.
    """
    cfg = load_settings()
    attempts = 2  # mirrors the retry loop in reply/enrichment.py
    worst_case = float(cfg.llm_tools_timeout_sec) * attempts
    assert worst_case <= 60.0, (
        f"memory extractor worst case is {worst_case:g}s across {attempts} attempts"
    )


# ── The extractor retry must not double a stall ──────────────────────
#
# Measured against a black-holed TCP endpoint: at a 15 s ceiling the old
# loop took 30.04 s (exactly 2 x the ceiling) and returned {} either way.
# The retry can only fix a fast, unparsable answer — not a stalled call.

import time as _time

import claire.reply.enrichment as _enrichment


def _extractor_cfg(model="some/model"):
    from types import SimpleNamespace

    return SimpleNamespace(
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:1/v1",
        llm_api_key="x",
        embedding_provider="",
        embedding_base_url="",
        embedding_api_key="",
        embedding_model="",
        ollama_base_url="http://127.0.0.1:11434",
    )


def test_stalled_extractor_is_not_retried(monkeypatch):
    """An attempt that consumes its whole budget is a stall, not a bad answer."""
    calls = []

    def _stall(*args, **kwargs):
        # Consume the whole ceiling, exactly as requests' read timeout does.
        calls.append(kwargs.get("timeout_sec"))
        _time.sleep(kwargs["timeout_sec"])
        return None

    monkeypatch.setattr(_enrichment, "call_llm_direct", _stall)

    result = _enrichment.extract_search_params_for_memory(
        "what's the weather tomorrow", _extractor_cfg(), "m", timeout_sec=0.05
    )

    assert result == {}
    assert len(calls) == 1, (
        f"a stalled extractor must not be retried; it called {len(calls)} times"
    )


def test_fast_unparsable_extractor_response_is_still_retried(monkeypatch):
    """The retry's real purpose survives: a quick non-JSON answer gets one more go."""
    calls = []

    def _bad(*args, **kwargs):
        calls.append(1)
        return "I am not JSON at all."

    monkeypatch.setattr(_enrichment, "call_llm_direct", _bad)

    result = _enrichment.extract_search_params_for_memory(
        "what's the weather tomorrow", _extractor_cfg(), "m", timeout_sec=5.0
    )

    assert result == {}
    assert len(calls) == 2, (
        f"a fast unparsable answer should still be retried; got {len(calls)} calls"
    )


def test_parsable_extractor_response_is_used_once(monkeypatch):
    """A good first answer is returned without a retry."""
    calls = []

    def _good(*args, **kwargs):
        calls.append(1)
        return '{"from": null, "to": null, "keywords": ["weather"], "questions": []}'

    monkeypatch.setattr(_enrichment, "call_llm_direct", _good)

    result = _enrichment.extract_search_params_for_memory(
        "what's the weather tomorrow", _extractor_cfg(), "m", timeout_sec=5.0
    )

    assert result["keywords"] == ["weather"]
    assert len(calls) == 1
