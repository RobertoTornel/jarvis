"""Unit tests for :mod:`claire.panel.server` (R-WEB-1).

The fail-proofs are the first two groups here:

* **A raw key value cannot be rendered.**  The page is rendered for *every*
  route with a state whose key store holds a known raw value, and the raw value
  must appear nowhere in the HTML — only its mask.  Then ``KeyStore.get`` — the
  codebase's single raw-value accessor — is patched to raise, and every route
  must still render with the mask, which is what proves the panel never reaches
  for a raw value in the first place.
* **A traversal path reads nothing.**  ``pathlib.Path.open`` is wrapped to
  record every file opened while the panel serves ``/../../etc/passwd`` (raw and
  percent-encoded); the request must be refused, nothing outside the injected
  roots may have been opened, and the recording is proven live by an ordinary
  ``/`` request that does open them.

Nothing here starts a server or opens a socket: every request is a
:class:`PanelRequest` handed to :func:`dispatch`.  The gate is
``pytest tests/ -m unit`` and it SILENTLY DESELECTS a file with no unit marker —
hence ``pytestmark = pytest.mark.unit`` below.  Do not remove it and do not add
this file to the guard's baseline list.
"""

import inspect
import json
import logging
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from claire.panel import server as panel
from claire.panel.server import (
    DEFAULT_BIND_HOST,
    DEFAULT_PORT,
    FORBIDDEN_KEY_FIELDS,
    GET_ROUTES,
    MAX_BODY_BYTES,
    ROUTE_HEALTH,
    ROUTE_INDEX,
    ROUTE_SELF_CHECK,
    ROUTE_SELECT,
    ActionRecord,
    ActionsStatus,
    CalendarEvent,
    CalendarStatus,
    ConfigCredential,
    KeyStatus,
    PanelBadRequest,
    PanelBindError,
    PanelMethodNotAllowed,
    PanelOriginError,
    PanelRequest,
    PanelRouteNotFound,
    PanelSecretError,
    PanelSourceError,
    PanelState,
    PanelStateError,
    PanelTooLarge,
    PanelTraversalError,
    PanelWriteError,
    ProviderModel,
    RealPanelState,
    RouterStatus,
    SelfCheckStatus,
    SelfCheckView,
    WriteResult,
    check_write_origin,
    create_server,
    dispatch,
    guard_bind_host,
    is_loopback_host,
    is_mask_shaped,
    parse_form,
    parse_target,
)
from claire.platform.keys import KeyStore, fingerprint, mask

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
PANEL_SOURCE = REPO_ROOT / "src" / "claire" / "panel" / "server.py"
SPEC = REPO_ROOT / "src" / "claire" / "panel" / "panel.spec.md"

#: The raw value that must never be seen.  Deliberately not a vendor-shaped
#: token: the guarantee must not depend on a scrub regex matching it.
RAW_KEY = "zzz-raw-secret-0123456789-DO-NOT-RENDER-zzz"
RAW_KEY_2 = "another-raw-secret-value-abcdef"
MASKED = mask(RAW_KEY)

FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Host": f"127.0.0.1:{DEFAULT_PORT}",
    "Origin": f"http://127.0.0.1:{DEFAULT_PORT}",
}


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

class FakeState(PanelState):
    """A complete fake: every section answers, nothing touches the disk."""

    def __init__(self, **overrides):
        self._overrides = overrides
        self.writes = []
        self.boom = None

    def _answer(self, name, default):
        if self.boom == name:
            raise RuntimeError(f"{name} exploded on purpose")
        return self._overrides.get(name, default)

    def provider_model(self):
        return self._answer(
            "provider_model",
            ProviderModel(
                provider="openai_compatible",
                model="cmd/deepseek/deepseek-v4.1-flash",
                base_url="http://127.0.0.1:20129/v1",
                source="config.json",
            ),
        )

    def key_status(self):
        return self._answer(
            "key_status",
            (
                KeyStatus(name="OMNIROUTE_API_KEY", present=True, render=MASKED, source="store"),
                KeyStatus(name="BRAVE_API_KEY", present=False),
            ),
        )

    def config_credentials(self):
        return self._answer("config_credentials", ())

    def router_status(self):
        return self._answer(
            "router_status",
            RouterStatus(
                path="/home/owner/.local/share/claire/vault/CLAIRE.md",
                exists=True,
                size_bytes=1200,
                budget_bytes=6000,
            ),
        )

    def calendar_today(self):
        return self._answer(
            "calendar_today",
            CalendarStatus(
                date="2026-01-31",
                path="/home/owner/.local/share/claire/schedule/schedule.json",
                existed=True,
                events=(
                    CalendarEvent(
                        title="Dentist",
                        date="2026-01-31",
                        start="16:00",
                        end="17:00",
                        duration_minutes=60,
                        notes="Dr Ruiz",
                    ),
                ),
            ),
        )

    def self_check(self):
        return self._answer(
            "self_check",
            SelfCheckStatus(
                ok=False,
                summary_line="4 ready, 1 missing, 0 problem - NOT READY",
                generated_at="2026-01-31T09:00:00Z",
                checks=(
                    SelfCheckView(name="vault", title="Memory vault", status="ready"),
                    SelfCheckView(
                        name="stt",
                        title="Speech model",
                        status="missing",
                        detail="no model file",
                        fix="Download the model.",
                    ),
                ),
            ),
        )

    def last_actions(self):
        return self._answer(
            "last_actions",
            ActionsStatus(
                path="/home/owner/.config/claire/actions.jsonl",
                records=(
                    ActionRecord(
                        at="2026-01-31T08:00:00Z",
                        action="send_signal",
                        outcome="sent",
                        detail="ladder reached Signal",
                    ),
                ),
            ),
        )

    def available_models(self):
        return ("cmd/deepseek/deepseek-v4.1-flash", "gemma4:e2b")

    def select_model(self, provider, model):
        self.writes.append((provider, model))
        return WriteResult(
            provider=provider, model=model, config_path="/tmp/config.json", changed=("llm_chat_model",)
        )


@pytest.fixture()
def state():
    return FakeState()


def fake_self_check(**kw):
    """A self-check stand-in, so no test ever runs a real probe.

    ``SelfCheck`` really does probe the machine (it reads ``/etc/os-release`` and
    speaks HTTP to the configured endpoint); the unit gate must not.
    """
    payload = {
        "ok": True,
        "summary_line": "5 ready, 0 missing, 0 problem - READY",
        "generated_at": "2026-01-31T09:00:00Z",
        "checks": (),
    }
    payload.update(kw)
    return lambda: SimpleNamespace(run=lambda: SimpleNamespace(**payload))


def real_state(**kw):
    """A ``RealPanelState`` with the real self-check replaced by a stand-in.

    Every ``RealPanelState`` in this file is built through here, so no unit test
    ever runs a live probe (which would read ``/etc/os-release`` and speak HTTP
    to the configured endpoint).
    """
    kw.setdefault("self_check_factory", fake_self_check())
    return RealPanelState(**kw)


def get(path, state, **kw):
    return dispatch(state, PanelRequest(method="GET", path=path, **kw))


def post(body, state, headers=None, **kw):
    return dispatch(
        state,
        PanelRequest(method="POST", path=ROUTE_SELECT, body=body, headers=headers or FORM_HEADERS, **kw),
    )


def all_route_html(state):
    """The rendered text of every route, keyed by route."""
    rendered = {}
    for route in GET_ROUTES:
        response = get(route, state)
        assert response.status in (200, 204), f"{route} rendered {response.status}"
        rendered[route] = response.text
    rendered[ROUTE_SELECT] = post(b"provider=ollama&model=gemma4:e2b", state).text
    return rendered


# --------------------------------------------------------------------------- #
# Fail-proof 1: a raw key value never reaches a page
# --------------------------------------------------------------------------- #

def test_no_raw_key_value_in_any_route(state):
    rendered = all_route_html(state)
    for route, text in rendered.items():
        assert RAW_KEY not in text, f"raw key value leaked into {route}"
        assert RAW_KEY[-8:] not in text, f"raw key tail leaked into {route}"
    index = rendered[ROUTE_INDEX]
    assert MASKED in index
    assert "absent" in index


def test_mask_is_last_four_plus_fingerprint(state):
    text = get(ROUTE_INDEX, state).text
    assert MASKED == f"...{RAW_KEY[-4:]}#{fingerprint(RAW_KEY)}"
    assert MASKED in text
    assert RAW_KEY[-4:] in text


def test_panel_never_calls_the_raw_value_accessor(state, monkeypatch, tmp_path):
    """``KeyStore.get`` raises: every route must still render, mask included."""
    import claire.platform.keys as keys_module

    def boom(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the panel called the raw-value accessor KeyStore.get")

    monkeypatch.setattr(keys_module.KeyStore, "get", boom)

    store_path = tmp_path / "keys.json"
    store_path.write_text(json.dumps({"version": 1, "keys": {"OMNIROUTE_API_KEY": RAW_KEY}}))
    store = KeyStore(store_path, env={})
    real = real_state(
        config_path=tmp_path / "config.json",
        key_store=store,
        router_path=tmp_path / "CLAIRE.md",
        schedule_path=tmp_path / "schedule.json",
        action_log_path=tmp_path / "actions.jsonl",
        environ={},
        now=lambda: date(2026, 1, 31),
    )
    for route in GET_ROUTES:
        response = get(route, real)
        assert response.status in (200, 204), f"{route} rendered {response.status}"
        assert RAW_KEY not in response.text, f"raw key value leaked into {route}"
    index = get(ROUTE_INDEX, real).text
    assert MASKED in index


def test_key_status_refuses_a_raw_value():
    with pytest.raises(PanelSecretError) as excinfo:
        KeyStatus(name="OMNIROUTE_API_KEY", present=True, render=RAW_KEY, source="store")
    assert "OMNIROUTE_API_KEY" in str(excinfo.value)
    assert "mask" in excinfo.value.fix


def test_key_status_refuses_a_raw_value_in_the_source():
    with pytest.raises(PanelSecretError):
        KeyStatus(name="K", present=True, render=MASKED, source=RAW_KEY)


def test_is_mask_shaped_accepts_only_real_masks():
    assert is_mask_shaped(mask(RAW_KEY))
    assert is_mask_shaped(mask("short"))
    assert is_mask_shaped(mask("x" * 40))
    # mask() reveals the last four characters by policy; they may be anything,
    # including a '#' or a space, and the tripwire must not reject that.
    assert is_mask_shaped(mask("abcdefgh# "))
    assert is_mask_shaped(mask("abcdefgh\ttab"))
    assert not is_mask_shaped(RAW_KEY)
    assert not is_mask_shaped("")
    assert not is_mask_shaped("...1234#nothex!!")
    assert not is_mask_shaped(MASKED + " extra")
    assert not is_mask_shaped("...ab#deadbeef")
    assert not is_mask_shaped("..ab#deadbeef")
    assert not is_mask_shaped("****#DEADBEEF")


def test_real_state_reports_a_corrupt_store_by_name(tmp_path):
    """A store that exists but cannot be read is a named error, never 'no keys'."""
    store_path = tmp_path / "keys.json"
    store_path.write_text(json.dumps({"OMNIROUTE_API_KEY": RAW_KEY}))  # wrong shape
    real = real_state(
        config_path=tmp_path / "config.json",
        key_store=KeyStore(store_path, env={}),
        environ={},
    )
    with pytest.raises(PanelSourceError) as excinfo:
        real.key_status()
    assert "key store" in excinfo.value.message
    assert RAW_KEY not in excinfo.value.message


def test_rendered_page_shows_only_the_mask_and_the_source(state):
    text = get(ROUTE_INDEX, state).text
    assert MASKED in text
    assert ">store<" in text
    assert "absent" in text


def test_config_credential_presence_is_shown_without_a_value():
    fake = FakeState(config_credentials=(ConfigCredential(name="llm_api_key", present=True),))
    text = get(ROUTE_INDEX, fake).text
    assert "llm_api_key" in text
    assert "value not shown" in text


def test_provider_row_reports_whether_a_key_is_stored():
    with_key = get(ROUTE_INDEX, FakeState()).text
    assert MASKED in with_key
    without_key = FakeState(key_status=(KeyStatus(name="OMNIROUTE_API_KEY", present=False),))
    assert "absent" in get(ROUTE_INDEX, without_key).text
    local = FakeState(provider_model=ProviderModel(provider="ollama", model="gemma4:e2b"))
    assert "not needed for this provider" in get(ROUTE_INDEX, local).text


def test_page_states_that_keys_are_set_out_of_band(state):
    text = get(ROUTE_INDEX, state).text
    assert "no key field on this page" in text
    assert "out-of-band" in text
    assert 'type="password"' not in text
    assert 'name="api_key"' not in text


def test_no_secret_appears_in_a_log_line(state, caplog):
    """The panel logs the method and the path, never a value."""

    class Probe(panel.PanelHandler):
        command = "POST"
        path = "/select?api_key=" + RAW_KEY

    with caplog.at_level(logging.DEBUG):
        Probe.log_message(object.__new__(Probe), "%s", RAW_KEY)
        post(b"provider=ollama&model=gemma4:e2b&api_key=" + RAW_KEY.encode(), state)
        post(b"provider=ollama&model=gemma4:e2b", state)
    assert RAW_KEY not in caplog.text
    assert RAW_KEY_2 not in caplog.text
    assert "panel POST /select" in caplog.text


def test_log_message_logs_only_the_scrubbed_path(caplog):
    class Probe(panel.PanelHandler):
        command = "GET"
        path = "/self-check?token=" + RAW_KEY

    with caplog.at_level(logging.DEBUG):
        Probe.log_message(object.__new__(Probe), "GET /self-check HTTP/1.1")
    assert RAW_KEY not in caplog.text
    assert "panel GET /self-check" in caplog.text


# --------------------------------------------------------------------------- #
# Routes: every one renders with injected state
# --------------------------------------------------------------------------- #

def test_every_read_route_renders_200(state):
    for route in GET_ROUTES:
        response = get(route, state)
        assert response.status in (200, 204), f"{route} -> {response.status}"


def test_index_renders_every_section(state):
    text = get(ROUTE_INDEX, state).text
    for marker in (
        "Provider and model",
        "Providers and keys",
        "Memory router (CLAIRE.md)",
        "Today&#x27;s calendar" if False else "calendar",
        "Self-check",
        "Last actions",
    ):
        assert marker in text, f"missing section: {marker}"
    assert "cmd/deepseek/deepseek-v4.1-flash" in text
    assert "Dentist" in text
    assert "16:00-17:00" in text
    assert "4 ready, 1 missing" in text
    assert "send_signal" in text
    assert "sent" in text


def test_self_check_route_is_plain_text(state):
    response = get(ROUTE_SELF_CHECK, state)
    assert response.status == 200
    assert response.content_type == "text/plain; charset=utf-8"
    assert "Summary: 4 ready, 1 missing, 0 problem - NOT READY" in response.text
    assert "fix: Download the model." in response.text


def test_health_route_is_json(state):
    response = get(ROUTE_HEALTH, state)
    assert response.status == 200
    assert response.content_type == "application/json; charset=utf-8"
    payload = json.loads(response.text)
    assert payload["ok"] is False
    assert payload["panel"] == panel.PANEL_VERSION
    assert payload["summary"].startswith("4 ready")


def test_favicon_is_empty_and_socketless(state):
    response = get("/favicon.ico", state)
    assert response.status == 204
    assert response.body == b""


def test_unknown_route_is_a_named_404(state):
    response = get("/definitely-not-a-route", state)
    assert response.status == 404
    assert "route-not-found" in response.text
    assert "Fix:" in response.text


def test_response_never_leaks_a_traceback(state):
    fake = FakeState()
    fake.boom = "calendar_today"
    response = get(ROUTE_INDEX, fake)
    assert response.status == 200
    assert "RuntimeError" in response.text
    assert "could not be read" in response.text
    assert "Traceback" not in response.text


def test_unimplemented_section_is_named_not_blank():
    class Partial(PanelState):
        pass

    response = get(ROUTE_INDEX, Partial())
    assert response.status == 200
    assert "state-missing" in response.text
    assert "provider_model" in response.text
    assert "panel state has no source for" in response.text


# --------------------------------------------------------------------------- #
# The memory router budget: under and over
# --------------------------------------------------------------------------- #

def _router_state(**kw):
    base = dict(
        path="/vault/CLAIRE.md",
        exists=True,
        size_bytes=1200,
        budget_bytes=6000,
    )
    base.update(kw)
    return FakeState(router_status=RouterStatus(**base))


def test_router_under_budget_renders_quietly():
    text = get(ROUTE_INDEX, _router_state()).text
    assert "1200 bytes of 6000 bytes" in text
    assert "within budget" in text
    assert "OVER BUDGET" not in text
    assert "TRUNCATED" not in text


def test_router_over_budget_renders_the_loud_framing():
    warning = (
        "!" * 72 + "\n"
        "!! WARNING: YOUR ALWAYS-LOADED MEMORY (CLAIRE.md) IS OVER BUDGET AND WAS TRUNCATED\n"
        "!! Real size: 7000 bytes. Budget: 6000 bytes. Dropped: 1000 bytes.\n"
        "!! Those dropped bytes are NOT being loaded: the end of the file is invisible to me.\n"
        "!! Fix it by moving detail into notes/ and keeping only one-line pointers here.\n"
        + "!" * 72
    )
    text = get(
        ROUTE_INDEX,
        _router_state(
            size_bytes=7000, budget_bytes=6000, over_budget=True, dropped_bytes=1000, warning=warning
        ),
    ).text
    assert "7000 bytes of 6000 bytes" in text
    assert "OVER BUDGET" in text
    assert "IS OVER BUDGET AND WAS TRUNCATED" in text
    assert "Real size: 7000 bytes" in text
    assert "Dropped: 1000 bytes" in text
    assert "notes/" in text
    assert 'class="bad"' in text


#: The path the store backend reports for the router; ``_router_section`` reads
#: the ``postgres`` prefix as "the router is a row, not a file".
_STORE_ROUTER_PATH = "postgres:memory_documents/router"


def test_store_router_over_budget_card_names_no_file_or_vault_notes():
    """The store over-budget card must not send the owner into a ``notes/`` folder.

    The router is a row in the store, so the ``CLAIRE.md`` filename and the
    vault's ``notes/`` directory are both wrong there.  The warning text comes
    from the store router; the shared "Fix" block below it must match.
    """
    text = get(
        ROUTE_INDEX,
        _router_state(
            path=_STORE_ROUTER_PATH,
            size_bytes=7000,
            budget_bytes=6000,
            over_budget=True,
            dropped_bytes=1000,
        ),
    ).text
    assert "7000 bytes of 6000 bytes" in text
    assert "OVER BUDGET" in text
    assert "CLAIRE.md" not in text
    assert "notes/" not in text


def test_vault_router_over_budget_card_still_names_the_vault_file():
    """The vault branch keeps its own text: ``CLAIRE.md`` and ``notes/`` are real.

    This is the counterweight to the store test above.  A fix that neutralised
    both branches would pass the store assertion and quietly break the vault,
    which is the backend ``CLAIRE_MEMORY_BACKEND=vault`` still selects.
    """
    text = get(
        ROUTE_INDEX,
        _router_state(
            path="/vault/CLAIRE.md",
            size_bytes=7000,
            budget_bytes=6000,
            over_budget=True,
            dropped_bytes=1000,
        ),
    ).text
    assert "CLAIRE.md" in text
    assert "notes/" in text


def test_real_router_warning_is_verbatim_the_prompt_framing(tmp_path):
    """The framing on the page is byte-for-byte the one the prompt prepends."""
    from claire.memory import router as router_module

    path = tmp_path / "CLAIRE.md"
    path.write_text("---\nbudget_bytes: 120\n---\n" + "\n".join(f"- fact {i}" for i in range(60)))
    real = real_state(router_path=path, environ={})
    status = real.router_status()
    assert status.over_budget is True
    assert status.warning.startswith("!" * 72)
    assert "IS OVER BUDGET AND WAS TRUNCATED" in status.warning

    loaded = router_module.load_router(path)
    rendered = loaded.render_for_prompt()
    assert status.warning == rendered.split(router_module.MEMORY_HEADER, 1)[0].strip()
    page = get(ROUTE_INDEX, real).text
    assert "IS OVER BUDGET" in page


def test_real_router_under_budget_reports_both_numbers(tmp_path):
    from claire.memory import router as router_module

    path = tmp_path / "CLAIRE.md"
    path.write_text("---\ntitle: t\n---\n\n# Pointers\n\n- dentist: notes/dentist.md\n")
    real = real_state(router_path=path, environ={})
    status = real.router_status()
    # ``size_bytes`` is the router's own accounting: the body bytes, which is
    # exactly what the 6000-byte budget (and the prompt) pays for.
    loaded = router_module.load_router(path)
    assert status.over_budget is False
    assert status.budget_bytes == router_module.ROUTER_BUDGET_BYTES == 6000
    assert status.size_bytes == loaded.size_bytes
    assert status.size_bytes < path.stat().st_size
    text = get(ROUTE_INDEX, real).text
    assert f"{status.size_bytes} bytes of 6000 bytes" in text
    assert "within budget" in text


def test_missing_router_is_named_with_a_fix_and_is_not_created(tmp_path):
    path = tmp_path / "CLAIRE.md"
    real = real_state(router_path=path, environ={})
    status = real.router_status()
    assert status.error and "RouterNotFoundError" in status.error
    assert "ensure_router" in (status.fix or "")
    assert not path.exists(), "the panel must never create the memory file"
    text = get(ROUTE_INDEX, real).text
    assert "RouterNotFoundError" in text


# --------------------------------------------------------------------------- #
# The self-check section
# --------------------------------------------------------------------------- #

def test_self_check_lists_non_ready_checks_with_fixes(state):
    text = get(ROUTE_INDEX, state).text
    assert "Speech model" in text
    assert "no model file" in text
    assert "Download the model." in text
    assert "MISSING" in text


def test_self_check_failure_is_named(state):
    def boom():
        raise RuntimeError("probes unavailable")

    fake = FakeState()
    fake.self_check = boom
    response = get(ROUTE_SELF_CHECK, fake)
    assert response.status == 200
    assert "self-check-unavailable" in response.text


def test_self_check_error_field_is_rendered_with_its_fix():
    fake = FakeState(
        self_check=SelfCheckStatus(
            error="ImportError: no probes", fix="Install the probes module."
        )
    )
    text = get(ROUTE_INDEX, fake).text
    assert "ImportError: no probes" in text
    assert "Install the probes module." in text


# --------------------------------------------------------------------------- #
# Last actions
# --------------------------------------------------------------------------- #

def test_actions_absent_journal_is_named_with_the_env_var_fix(tmp_path):
    real = real_state(
        action_log_path=tmp_path / "actions.jsonl", environ={}, config_path=tmp_path / "c.json"
    )
    status = real.last_actions()
    assert status.records == ()
    assert "no action journal" in (status.error or "")
    assert panel.ACTION_LOG_ENV_VAR in (status.fix or "")
    text = get(ROUTE_INDEX, real).text
    assert "no action journal" in text
    assert panel.ACTION_LOG_ENV_VAR in text


def test_actions_journal_tail_is_read_and_bad_lines_are_counted(tmp_path):
    path = tmp_path / "actions.jsonl"
    lines = [
        json.dumps({"at": f"t{i}", "action": f"act{i}", "outcome": "ok", "detail": f"d{i}"})
        for i in range(12)
    ]
    path.write_text("\n".join(lines[:5]) + "\nnot json\n" + "\n".join(lines[5:]) + "\n")
    real = real_state(action_log_path=path, environ={})
    status = real.last_actions()
    assert len(status.records) == real.ACTION_LIMIT
    assert status.records[-1].action == "act11"
    assert status.skipped_lines == 1
    text = get(ROUTE_INDEX, real).text
    assert "act11" in text
    assert "1 journal line(s) were not readable JSON" in text


def test_action_detail_is_scrubbed_of_a_vendor_shaped_token():
    token = "sk-" + "a" * 40
    record = ActionRecord(at="t", action="call", outcome="ok", detail=f"Authorization: Bearer {token}")
    assert token not in record.detail


# --------------------------------------------------------------------------- #
# The selector form and the write endpoint
# --------------------------------------------------------------------------- #

def test_selector_form_is_present_and_has_no_key_field(state):
    text = get(ROUTE_INDEX, state).text
    assert '<form method="post" action="/select">' in text
    assert 'name="provider"' in text
    assert 'name="model"' in text
    assert "gemma4:e2b" in text
    for name in FORBIDDEN_KEY_FIELDS:
        assert f'name="{name}"' not in text
    assert '<button type="submit">Save selection</button>' in text


def test_valid_select_writes_and_redirects(state):
    response = post(b"provider=ollama&model=gemma4:e2b", state)
    assert response.status == 303
    assert dict(response.headers)["Location"] == "/?changed=1"
    assert state.writes == [("ollama", "gemma4:e2b")]


def test_changed_flag_renders_a_confirmation(state):
    assert "Selection saved." in get("/?changed=1", state).text
    assert "Selection saved." not in get("/", state).text


def test_select_without_origin_is_refused_and_writes_nothing(state):
    response = dispatch(
        state,
        PanelRequest(
            method="POST",
            path=ROUTE_SELECT,
            body=b"provider=ollama&model=gemma4:e2b",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Host": "127.0.0.1"},
        ),
    )
    assert response.status == 403
    assert "origin-refused" in response.text
    assert state.writes == []


def test_cross_origin_select_is_refused(state):
    response = post(
        b"provider=ollama&model=gemma4:e2b",
        state,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "http://evil.example.com",
            "Host": f"127.0.0.1:{DEFAULT_PORT}",
        },
    )
    assert response.status == 403
    assert "cross-origin" in response.text
    assert state.writes == []


def test_mismatched_host_is_refused(state):
    response = post(
        b"provider=ollama&model=gemma4:e2b",
        state,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "http://127.0.0.1:1",
            "Host": f"127.0.0.1:{DEFAULT_PORT}",
        },
    )
    assert response.status == 403
    assert state.writes == []


def test_select_from_a_different_port_is_refused(state):
    response = post(
        b"provider=ollama&model=gemma4:e2b",
        state,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "http://127.0.0.1:9999",
            "Host": f"127.0.0.1:{DEFAULT_PORT}",
        },
        server_port=DEFAULT_PORT,
    )
    assert response.status == 403
    assert state.writes == []


def test_sec_fetch_site_cross_site_is_refused(state):
    response = post(
        b"provider=ollama&model=gemma4:e2b",
        state,
        headers=dict(FORM_HEADERS, **{"Sec-Fetch-Site": "cross-site"}),
    )
    assert response.status == 403
    assert state.writes == []


@pytest.mark.parametrize("field", sorted(FORBIDDEN_KEY_FIELDS))
def test_key_shaped_field_is_refused(state, field):
    body = f"provider=ollama&model=gemma4:e2b&{field}={RAW_KEY}".encode()
    response = post(body, state)
    assert response.status == 403
    assert field in response.text
    assert RAW_KEY not in response.text
    assert "out-of-band" in response.text
    assert state.writes == []


def test_select_with_nothing_to_write_is_a_named_refusal(state):
    response = post(b"", state)
    assert response.status == 400
    assert "provider" in response.text
    assert state.writes == []


def test_unknown_provider_is_refused(state):
    response = post(b"provider=not-a-provider&model=gemma4:e2b", state)
    assert response.status == 400
    assert "unknown provider" in response.text
    assert state.writes == []


@pytest.mark.parametrize("model", ["bad model", "x" * 200, "-leading", "a;b", ""])
def test_invalid_model_ids_are_refused(state, model):
    body = f"provider=ollama&model={model}".encode()
    response = post(body, state)
    assert response.status == 400
    assert state.writes == []


def test_get_on_select_is_method_not_allowed(state):
    response = get(ROUTE_SELECT, state)
    assert response.status == 405
    assert "method-not-allowed" in response.text


def test_post_on_a_read_route_is_method_not_allowed(state):
    response = dispatch(state, PanelRequest(method="POST", path=ROUTE_INDEX, body=b""))
    assert response.status == 405
    assert "method-not-allowed" in response.text


def test_oversized_body_is_refused(state):
    request = PanelRequest(
        method="POST",
        path=ROUTE_SELECT,
        body=b"provider=ollama&model=gemma4:e2b",
        headers=FORM_HEADERS,
        declared_length=MAX_BODY_BYTES + 1,
    )
    response = dispatch(state, request)
    assert response.status == 413
    assert "body-too-large" in response.text
    assert state.writes == []


def test_non_form_content_type_is_refused(state):
    response = post(
        b"provider=ollama&model=gemma4:e2b",
        state,
        headers=dict(FORM_HEADERS, **{"Content-Type": "text/plain"}),
    )
    assert response.status == 400
    assert state.writes == []


def test_write_failure_is_a_named_panel_error():
    class Broken(PanelState):
        def provider_model(self):
            return ProviderModel(provider="ollama", model="m")

        def available_providers(self):
            return ("ollama",)

        def select_model(self, provider, model):
            raise OSError("read-only file system")

    response = post(b"provider=ollama&model=gemma4:e2b", Broken())
    assert response.status == 500
    assert "write-failed" in response.text
    assert "read-only file system" in response.text


def test_real_select_model_writes_provider_and_model(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"llm_provider": "openai_compatible", "llm_chat_model": "old"}))
    real = real_state(config_path=config, environ={})
    result = real.select_model("ollama", "gemma4:e2b")
    assert "llm_provider" in result.changed
    saved = json.loads(config.read_text())
    assert saved["llm_provider"] == "ollama"
    assert saved["ollama_chat_model"] == "gemma4:e2b"
    # An unchanged selection reports that nothing changed.
    again = real.select_model("ollama", "gemma4:e2b")
    assert again.changed == ()


def test_real_provider_model_mirrors_config_resolution(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"llm_provider": "ollama", "ollama_chat_model": "gemma4:e2b"})
    )
    real = real_state(config_path=config, environ={})
    info = real.provider_model()
    assert info.provider == "ollama"
    assert info.model == "gemma4:e2b"


def test_real_state_selector_shows_the_current_model(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"llm_provider": "omniroute", "llm_chat_model": "custom/model-1"}))
    real = real_state(config_path=config, environ={})
    info = real.provider_model()
    assert info.provider == "openai_compatible"
    assert info.model == "custom/model-1"
    assert "custom/model-1" in get(ROUTE_INDEX, real).text


# --------------------------------------------------------------------------- #
# Traversal: refused, and nothing outside is read
# --------------------------------------------------------------------------- #

TRAVERSAL_PATHS = [
    "/../etc/passwd",
    "/../../etc/passwd",
    "/%2e%2e/%2e%2e/etc/passwd",
    "/..%2f..%2fetc/passwd",
    "/./../../etc/passwd",
    "/a/../../etc/passwd",
    "/etc/passwd",
    "/..\\..\\etc\\passwd",
    "/%2e%2e\\etc\\passwd",
]


@pytest.mark.parametrize("path", TRAVERSAL_PATHS)
def test_traversal_paths_are_refused(state, path):
    response = get(path, state)
    assert response.status in (400, 404), f"{path} -> {response.status}"
    if response.status == 400:
        assert "traversal-refused" in response.text
    else:
        assert "route-not-found" in response.text


@pytest.mark.parametrize("path", TRAVERSAL_PATHS)
def test_traversal_paths_serve_no_file_content(state, path):
    """Nothing outside the injected roots can be reached, so nothing leaks."""
    response = get(path, state)
    assert "root:x:0:0" not in response.text
    assert "<script>" not in response.text


def test_traversal_refusal_reads_nothing_outside(tmp_path, monkeypatch):
    """Record every file the panel opens while serving traversal attempts.

    Two assertions make this a real fail-proof rather than a vacuous one: the
    ordinary ``/`` request must show up in the recording (so we know the
    recorder sees the panel's reads at all), and the traversal attempts must
    record *nothing* — the refusal page never consults the state, so no file is
    opened and nothing outside the injected roots can leak.
    """
    opened = []
    real_open = Path.open

    def recording_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)

    (tmp_path / "config.json").write_text(json.dumps({"llm_provider": "ollama"}))
    (tmp_path / "CLAIRE.md").write_text("---\ntitle: t\n---\n\n# Pointers\n\n- x: notes/x.md\n")
    (tmp_path / "schedule.json").write_text(json.dumps({"version": "1", "events": []}))
    (tmp_path / "actions.jsonl").write_text(json.dumps({"at": "t", "action": "a", "outcome": "ok"}) + "\n")
    sentinel = tmp_path / "etc" / "passwd"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("root:x:0:0:SENTINEL-NOT-TO-BE-READ:/root:/bin/bash\n")

    real = real_state(
        config_path=tmp_path / "config.json",
        key_store=KeyStore(tmp_path / "keys.json", env={}),
        router_path=tmp_path / "CLAIRE.md",
        schedule_path=tmp_path / "schedule.json",
        action_log_path=tmp_path / "actions.jsonl",
        environ={},
        now=lambda: date(2026, 1, 31),
        self_check_factory=fake_self_check(),
    )

    opened.clear()
    assert get(ROUTE_INDEX, real).status == 200
    assert opened, "the recorder did not observe the panel's own reads"
    assert all(str(tmp_path) in path for path in opened), opened

    opened.clear()
    for path in TRAVERSAL_PATHS:
        response = get(path, real)
        assert response.status in (400, 404)
        assert "SENTINEL-NOT-TO-BE-READ" not in response.text
    assert opened == [], f"a traversal request read: {opened}"
    assert all("passwd" not in path for path in opened), opened


def test_absolute_form_target_is_refused(state):
    response = get("http://example.com/etc/passwd", state)
    assert response.status == 400
    assert "absolute URL" in response.text


def test_nul_and_backslash_targets_are_refused(state):
    for path in ("/a\x00b", "/a\\b"):
        response = get(path, state)
        assert response.status == 400
        assert "traversal-refused" in response.text


def test_parse_target_normalises_and_refuses():
    assert parse_target("/")[0] == "/"
    assert parse_target("/self-check?x=1")[0] == "/self-check"
    assert parse_target("/a//b/")[0] == "/a/b"
    with pytest.raises(PanelTraversalError):
        parse_target("/../etc/passwd")
    with pytest.raises(PanelTraversalError):
        parse_target("/%2e%2e/etc/passwd")
    with pytest.raises(PanelBadRequest):
        parse_target("")


def test_dispatch_uses_a_fixed_route_table():
    """Routes are exact strings; nothing builds a path from the request."""
    assert set(panel.GET_VIEWS) <= set(GET_ROUTES)
    source = PANEL_SOURCE.read_text(encoding="utf-8")
    assert "os.path.join" not in source
    assert "sendfile" not in source
    assert "os.open" not in source
    assert not re.search(r"Path\(\s*request", source)
    assert not re.search(r"open\(\s*path\s*\)", source)


# --------------------------------------------------------------------------- #
# Escaping
# --------------------------------------------------------------------------- #

XSS = "<script>alert('xss')</script>"


def test_html_in_data_values_is_escaped_everywhere():
    fake = FakeState(
        provider_model=ProviderModel(provider=XSS, model=XSS, base_url=XSS, source=XSS),
        key_status=(KeyStatus(name=XSS, present=True, render=mask(RAW_KEY), source="store"),),
        router_status=RouterStatus(path=XSS, exists=True, size_bytes=1, budget_bytes=6000),
        calendar_today=CalendarStatus(
            date=XSS, path=XSS, existed=True, events=(CalendarEvent(title=XSS, notes=XSS, start=XSS),)
        ),
        self_check=SelfCheckStatus(
            ok=False,
            summary_line=XSS,
            generated_at=XSS,
            checks=(SelfCheckView(name=XSS, title=XSS, status="missing", detail=XSS, fix=XSS),),
        ),
        last_actions=ActionsStatus(
            path=XSS, records=(ActionRecord(at=XSS, action=XSS, outcome=XSS, detail=XSS),)
        ),
    )
    for route in GET_ROUTES:
        response = get(route, fake)
        assert response.status in (200, 204)
        if not response.content_type.startswith("text/html"):
            # Served as text/plain or application/json with nosniff: a browser
            # never parses these as markup, and escaping them would corrupt the
            # text the owner asked to read.
            assert response.content_type in (
                "text/plain; charset=utf-8",
                "application/json; charset=utf-8",
                "image/x-icon",
            )
            continue
        # The index carries exactly one script pair: the panel's own static face
        # poller (docs/PANEL_FACE.md), which interpolates no data.  Every other
        # page must carry none.  Counting, rather than banning the substring,
        # keeps the original guarantee intact: injected data values can never
        # open a script, because the count would move off its fixed number.
        own_scripts = 1 if route == ROUTE_INDEX else 0
        assert response.text.count("<script>") == own_scripts, f"unexpected script in {route}"
        assert response.text.count("</script>") == own_scripts, (
            f"unexpected closing script in {route}"
        )
    index = get(ROUTE_INDEX, fake).text
    assert "&lt;script&gt;" in index
    assert "&#x27;" in index or "&#39;" in index


def test_non_html_routes_cannot_be_parsed_as_markup():
    fake = FakeState(self_check=SelfCheckStatus(ok=True, summary_line=XSS, generated_at=XSS))
    text_route = get(ROUTE_SELF_CHECK, fake)
    assert text_route.content_type == "text/plain; charset=utf-8"
    assert ("X-Content-Type-Options", "nosniff") in text_route.headers
    health = get(ROUTE_HEALTH, fake)
    assert health.content_type == "application/json; charset=utf-8"
    assert ("X-Content-Type-Options", "nosniff") in health.headers
    assert "<script>" not in health.text
    # Every ``<`` becomes \u003c, so neither a tag nor a closing tag can form.
    assert "\\u003cscript" in health.text
    assert "</script>" not in health.text


def test_error_pages_escape_the_message():
    exc = PanelTraversalError(f"the request target {XSS!r} contains a traversal segment.")
    response = panel.render_error_page(exc)
    assert response.status == 400
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_query_values_are_not_reflected(state):
    response = get(f"/?changed=1&x={XSS}", state)
    assert response.status == 200
    # The index carries exactly the panel's own static face poller
    # (docs/PANEL_FACE.md); a reflected query value would open a second script.
    assert response.text.count("<script>") == 1
    assert XSS not in response.text


def test_selector_does_not_reflect_a_bad_model_into_the_page():
    fake = FakeState(provider_model=ProviderModel(provider="ollama", model=XSS))
    text = get(ROUTE_INDEX, fake).text
    # Exactly the panel's own poll script, never a reflected one: the payload
    # reaches the page only in its escaped form.
    assert text.count("<script>") == 1
    assert "&lt;script&gt;" in text
    assert "<script>alert" not in text


# --------------------------------------------------------------------------- #
# Bind host
# --------------------------------------------------------------------------- #

def test_default_bind_host_is_loopback():
    assert DEFAULT_BIND_HOST == "127.0.0.1"
    assert is_loopback_host(DEFAULT_BIND_HOST)
    signature = inspect.signature(create_server)
    assert signature.parameters["host"].default == DEFAULT_BIND_HOST
    assert signature.parameters["allow_remote"].default is False


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost", "[::1]"])
def test_loopback_hosts_are_accepted(host):
    assert is_loopback_host(host)
    assert guard_bind_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "", "example.com", "10.0.0.1"])
def test_non_loopback_hosts_are_refused(host):
    assert not is_loopback_host(host)
    with pytest.raises(PanelBindError) as excinfo:
        guard_bind_host(host)
    assert "--allow-remote-binding" in excinfo.value.fix


def test_non_loopback_bind_is_refused_before_any_socket():
    with pytest.raises(PanelBindError):
        create_server(FakeState(), "0.0.0.0", DEFAULT_PORT)


def test_remote_bind_opt_in_warns_loudly(caplog):
    with caplog.at_level(logging.WARNING):
        assert guard_bind_host("0.0.0.0", allow_remote=True) == "0.0.0.0"
    assert "PLAIN HTTP" in caplog.text
    assert "0.0.0.0" in caplog.text


def test_check_write_origin_accepts_the_panel_page():
    request = PanelRequest(method="POST", path=ROUTE_SELECT, headers=FORM_HEADERS)
    assert check_write_origin(request) == f"http://127.0.0.1:{DEFAULT_PORT}"


def test_check_write_origin_rejects_https_and_odd_schemes():
    for origin in ("https://127.0.0.1", "file://127.0.0.1", "null"):
        request = PanelRequest(
            method="POST",
            path=ROUTE_SELECT,
            headers=dict(FORM_HEADERS, **{"Origin": origin}),
        )
        with pytest.raises(PanelOriginError):
            check_write_origin(request)


def test_an_opaque_origin_is_accepted_only_with_same_origin_fetch_metadata():
    """The panel's own form must be able to save, without weakening the guard.

    This is the bug the owner hit: the panel answered its own form with
    "Origin: null" and the guard refused every genuine write, so no setting could
    ever be saved - while the panel looked entirely healthy.

    An opaque origin proves nothing by itself (a cross-site attacker gets exactly
    the same "null"), so it is accepted ONLY alongside Sec-Fetch-Site:
    same-origin, which the browser derives from the real initiator and a foreign
    page cannot forge. Both halves are asserted here, because accepting the first
    without the second would be no CSRF protection at all.
    """
    accepted = PanelRequest(
        method="POST",
        path=ROUTE_SELECT,
        headers=dict(FORM_HEADERS, **{
            "Origin": "null",
            "Sec-Fetch-Site": "same-origin",
        }),
    )
    assert check_write_origin(accepted) == "same-origin"

    # Same opaque origin, but the fetch metadata says it did NOT come from this
    # page: refused.
    for site in ("cross-site", "same-site"):
        forged = PanelRequest(
            method="POST",
            path=ROUTE_SELECT,
            headers=dict(FORM_HEADERS, **{
                "Origin": "null",
                "Sec-Fetch-Site": site,
            }),
        )
        with pytest.raises(PanelOriginError):
            check_write_origin(forged)


def test_the_panel_does_not_set_a_referrer_policy_that_blinds_its_own_guard():
    """Pin the root cause, not just the symptom.

    Referrer-Policy: no-referrer makes the browser send an opaque "Origin: null"
    on the panel's own form POST. Combined with an origin guard that is
    self-sabotage: the hardening is precisely what breaks the writes. The policy
    must stay same-origin, which keeps the referrer off OTHER sites (the actual
    intent) while letting our own requests carry a real Origin.
    """
    source = inspect.getsource(panel)
    assert '"Referrer-Policy", "no-referrer"' not in source
    assert '"Referrer-Policy", "same-origin"' in source
    assert 'content="same-origin"' in source


# --------------------------------------------------------------------------- #
# Housekeeping: markers, exports, spec
# --------------------------------------------------------------------------- #

def test_every_panel_error_names_a_code_and_an_actionable_fix():
    """The project rule: a missing thing is a NAMED error with a fix, never silence."""
    errors = [
        PanelBadRequest("the request could not be read"),
        PanelTraversalError("the target tried to walk out of the routes"),
        PanelRouteNotFound("/nope"),
        PanelMethodNotAllowed("PUT", "/", allowed=("GET",)),
        PanelOriginError("the origin is not loopback"),
        PanelWriteError("writing config.json failed"),
        PanelBindError("0.0.0.0"),
        PanelStateError("provider_model"),
        PanelSecretError("OMNIROUTE_API_KEY"),
        PanelSourceError("the key store", RuntimeError("boom")),
        PanelTooLarge("the body is too big"),
    ]
    for exc in errors:
        assert exc.code and exc.code == exc.code.lower(), exc
        assert exc.message, exc
        assert exc.fix and len(exc.fix) > 10, exc
        assert exc.status in (400, 403, 404, 405, 413, 500), exc
        assert set(exc.to_dict()) == {"code", "status", "message", "fix"}
        assert exc.to_dict()["message"] == exc.message


def test_parse_form_refuses_an_oversized_declared_body():
    request = PanelRequest(
        method="POST",
        path=ROUTE_SELECT,
        body=b"a=1",
        headers=FORM_HEADERS,
        declared_length=MAX_BODY_BYTES + 1,
    )
    with pytest.raises(PanelTooLarge):
        parse_form(request)


def test_route_not_found_names_the_routes_it_does_serve():
    exc = PanelRouteNotFound("/nope")
    assert exc.status == 404
    for route in GET_ROUTES:
        assert route in exc.fix


def test_cli_help_works_and_binds_nothing(capsys):
    with pytest.raises(SystemExit) as excinfo:
        panel.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--allow-remote-binding" in out
    assert "--port" in out
    assert "--action-log" in out


def test_serve_is_the_only_socket_opener():
    source = PANEL_SOURCE.read_text(encoding="utf-8")
    # ``create_server`` builds (and guards) but never calls serve_forever;
    # exactly one place in the module starts listening.
    assert source.count("serve_forever(") == 1
    assert source.count("PanelServer((") == 1


def test_this_file_carries_the_unit_marker():
    source = Path(__file__).read_text(encoding="utf-8")
    assert "pytestmark = pytest.mark.unit" in source


def test_panel_package_exports_what_it_documents():
    import claire.panel as package

    for name in package.__all__:
        assert hasattr(package, name), name
    for name in panel.__all__:
        assert hasattr(panel, name), name


def test_spec_exists_and_names_the_security_contract():
    text = SPEC.read_text(encoding="utf-8")
    for marker in (
        "loopback",
        "raw key",
        "traversal",
        "Origin",
        "plain HTTP",
        "escaped",
    ):
        assert marker.lower() in text.lower(), marker


def test_real_state_has_no_import_time_side_effects():
    """Importing the panel must not touch the owner's files."""
    source = PANEL_SOURCE.read_text(encoding="utf-8")
    module_level = source.split("\n\n\nclass PanelError", 1)[0]
    assert "default_config_path()" not in module_level
    assert "default_store_path()" not in module_level
    assert not re.search(r"^if __name__ == .__main__.:", module_level, re.MULTILINE)
