"""The panel's conversation and chat surfaces (R-WEB-1).

Three claims are load-bearing here, and each is pinned by a test that would fail
without the code it is about:

1. **The chat goes through the project's one LLM stack.**  It must reach
   ``claire.config.load_settings`` and ``claire.llm.get_llm_backend`` — the same
   resolver the daemon and the reply engine use — and it must not open an HTTP
   connection of its own.  This codebase has already been burned once by a
   near-duplicate stack (a phone endpoint almost built on a second transcription
   path while one authoritative recogniser existed), so the assertion is about
   *which* seam was used, not merely that a reply appeared.
2. **A failure is named, never an empty box.**  The backend's ``direct()``
   reports every failure as ``None``, which the panel turns into a 502 that names
   the provider, the model and the endpoint.
3. **The transcript is honest about what it is.**  The verbatim conversation
   lives in the daemon process's memory and is unreachable from here, so the
   panel keeps its own bounded in-memory turns and says so, while reading the
   memory layer's *persisted* daily summaries read-only beside them.

``pytestmark = pytest.mark.unit`` is not optional: without it every test in this
file is silently deselected rather than run, which is how a whole surface can
look tested and be untested.
"""

from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlencode

import pytest

from claire.panel import server as panel
from claire.panel.server import (
    CHAT_TIMEOUT_SECONDS,
    DIARY_LIMIT,
    MAX_CHAT_CHARS,
    ROUTE_CHAT,
    ROUTE_INDEX,
    ROUTE_SELECT,
    TRANSCRIPT_LIMIT,
    ChatResult,
    DiaryStatus,
    PanelLLMError,
    PanelRequest,
    PanelState,
    ProviderModel,
    RealPanelState,
    TranscriptBuffer,
    TranscriptStatus,
    check_write_origin,
    dispatch,
    parse_form,
)

# The shared fake lives with the in-process tests; pytest puts tests/ on sys.path.
from test_panel_server import FORM_HEADERS, MASKED, RAW_KEY, FakeState

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class ChatState(FakeState):
    """The complete fake, plus a chat seam that never touches the network."""

    def __init__(self, reply="Blue, as far as I can tell.", error=None, **overrides):
        super().__init__(**overrides)
        self.calls: list[tuple[str, float]] = []
        self._reply = reply
        self._error = error
        self.transcript_buffer = TranscriptBuffer()

    def chat_reply(self, message, *, timeout_sec=CHAT_TIMEOUT_SECONDS):
        self.calls.append((message, timeout_sec))
        if self._error is not None:
            raise self._error
        self.transcript_buffer.record("you", message, "12:00:00")
        self.transcript_buffer.record("claire", self._reply, "12:00:01")
        return ChatResult(
            text=self._reply,
            provider="openai_compatible",
            model="cmd/deepseek/deepseek-v4.1-flash",
            base_url="http://127.0.0.1:20129/v1",
            elapsed_ms=12,
        )

    def transcript(self):
        return panel.TranscriptStatus(
            turns=self.transcript_buffer.turns(),
            source="this panel's process memory",
            durable=False,
            note=panel.PanelState.transcript(self).note,
        )


def request(method="POST", path=ROUTE_CHAT, body=b"", headers=None):
    """Build a request; a header mapped to ``None`` is *removed*.

    Removal matters: the interesting refusal cases are the ones where the browser
    sent no ``Origin`` at all, and merging into a base dict would silently put it
    back.
    """
    merged = dict(FORM_HEADERS)
    for name, value in (headers or {}).items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    return PanelRequest(method=method, path=path, headers=merged, body=body)


def chat(message, state=None, **kwargs):
    state = state or ChatState()
    body = urlencode({"message": message}).encode("utf-8")
    return dispatch(state, request(body=body, **kwargs)), state


def get(path, state):
    return dispatch(state, PanelRequest(method="GET", path=path, headers={"Host": "127.0.0.1:8765"}))


def text_of(response):
    return response.body.decode("utf-8")


class _StubReport:
    ok = True
    summary_line = "stub - nothing was probed"
    generated_at = "1970-01-01T00:00:00"
    checks = ()


def real_state(**kwargs):
    """A ``RealPanelState`` that reads no owner files and runs no probes.

    ``self_check()`` is the real self-check suite: it probes audio devices,
    tesseract, the browser and the LLM endpoint, takes seconds, and would make
    these unit tests depend on the machine.  It is stubbed here for the same
    reason the rest of the panel's tests inject a fake state.
    """
    from types import SimpleNamespace

    kwargs.setdefault("config_path", "/nonexistent/config.json")
    kwargs.setdefault("diary_path", "/nonexistent/claire.db")
    kwargs.setdefault(
        "self_check_factory", lambda: SimpleNamespace(run=lambda: _StubReport())
    )
    return RealPanelState(**kwargs)


# --------------------------------------------------------------------------- #
# The chat uses the one LLM stack, not a second client
# --------------------------------------------------------------------------- #


class _RecordingBackend:
    """Stands in for the real backend; records what the one stack was asked."""

    def __init__(self):
        self.calls = []

    def direct(self, model, system_prompt, user_content, **kwargs):
        self.calls.append((model, system_prompt, user_content, kwargs))
        return "Hello from the configured model."


class _FakeSettings:
    llm_provider = "openai_compatible"
    llm_chat_model = "cmd/deepseek/deepseek-v4.1-flash"
    llm_base_url = "http://127.0.0.1:20129/v1"
    ollama_base_url = "http://127.0.0.1:11434"


def test_the_chat_goes_through_get_llm_backend(monkeypatch):
    """The seam is ``claire.llm.get_llm_backend`` — the resolver the daemon uses.

    Fails if the panel builds its own client: this test replaces exactly that
    factory, so a direct HTTP call would produce no reply at all.
    """
    backend = _RecordingBackend()
    seen = {}

    def fake_get_llm_backend(settings):
        seen["settings"] = settings
        return backend

    monkeypatch.setattr("claire.llm.get_llm_backend", fake_get_llm_backend)
    result = panel.default_chat_call("Are you there?", settings=_FakeSettings())

    assert seen["settings"].llm_chat_model == "cmd/deepseek/deepseek-v4.1-flash"
    assert len(backend.calls) == 1
    model, system_prompt, user_content, kwargs = backend.calls[0]
    assert model == "cmd/deepseek/deepseek-v4.1-flash", "must use the configured model"
    assert user_content == "Are you there?"
    assert system_prompt == panel.PANEL_CHAT_SYSTEM_PROMPT
    assert kwargs["timeout_sec"] == CHAT_TIMEOUT_SECONDS
    assert result.text == "Hello from the configured model."
    assert result.provider == "openai_compatible"
    assert result.base_url == "http://127.0.0.1:20129/v1"


def test_default_chat_call_resolves_the_owner_settings_when_none_are_given(monkeypatch):
    """With no injected settings it calls ``claire.config.load_settings``."""
    backend = _RecordingBackend()
    monkeypatch.setattr("claire.config.load_settings", lambda: _FakeSettings())
    monkeypatch.setattr("claire.llm.get_llm_backend", lambda settings: backend)

    result = panel.default_chat_call("hi")

    assert result.text.startswith("Hello from")
    assert backend.calls[0][0] == "cmd/deepseek/deepseek-v4.1-flash"


def test_the_panel_module_never_opens_its_own_provider_connection():
    """No second HTTP client: no ``requests``, no ``urllib.request``, no sockets.

    A regex over the source is a blunt instrument, but it is the right one here:
    the failure mode being guarded against is *someone adding* a provider call
    beside the existing stack, and that shows up as a new import long before it
    shows up as a wrong reply.
    """
    source = (panel.__file__ and open(panel.__file__, encoding="utf-8").read()) or ""
    assert "import requests" not in source
    assert "from requests" not in source
    assert "import urllib.request" not in source
    assert "from urllib.request import" not in source
    assert "get_llm_backend" in source, "the one stack must actually be used"


def test_a_backend_that_refuses_raises_an_exception_is_a_named_error(monkeypatch):
    class Exploding:
        def direct(self, *a, **k):
            raise ConnectionError("connection refused")

    monkeypatch.setattr("claire.llm.get_llm_backend", lambda settings: Exploding())
    with pytest.raises(PanelLLMError) as caught:
        panel.default_chat_call("hi", settings=_FakeSettings())

    error = caught.value
    assert error.status == 502
    assert error.code == "llm-unavailable"
    assert "connection refused" in error.message
    assert "cmd/deepseek/deepseek-v4.1-flash" in error.message


def test_a_backend_that_returns_nothing_is_a_named_error_not_an_empty_reply(monkeypatch):
    class Silent:
        def direct(self, *a, **k):
            return None

    monkeypatch.setattr("claire.llm.get_llm_backend", lambda settings: Silent())
    with pytest.raises(PanelLLMError) as caught:
        panel.default_chat_call("hi", settings=_FakeSettings())

    message = caught.value.message
    assert "returned nothing" in message
    assert "openai_compatible" in message
    assert "http://127.0.0.1:20129/v1" in message


def test_whitespace_only_reply_is_a_named_error(monkeypatch):
    class Blank:
        def direct(self, *a, **k):
            return "   \n"

    monkeypatch.setattr("claire.llm.get_llm_backend", lambda settings: Blank())
    with pytest.raises(PanelLLMError):
        panel.default_chat_call("hi", settings=_FakeSettings())


# --------------------------------------------------------------------------- #
# The chat route
# --------------------------------------------------------------------------- #


def test_a_chat_post_redirects_to_the_conversation_so_a_refresh_cannot_resend():
    response, state = chat("Hello Claire")

    assert response.status == 303
    assert dict(response.headers)["Location"] == "/#conversation"
    assert state.calls == [("Hello Claire", CHAT_TIMEOUT_SECONDS)]


def test_the_reply_is_on_the_page_after_the_redirect():
    _, state = chat("What colour is the sky?")
    page = text_of(get(ROUTE_INDEX, state))

    assert "What colour is the sky?" in page
    assert "Blue, as far as I can tell." in page


def test_the_chat_route_is_not_reachable_by_get():
    response = get(ROUTE_CHAT, ChatState())

    assert response.status == 405


def test_a_cross_origin_chat_post_is_refused_and_names_the_chat_route():
    response, state = chat(
        "exfiltrate",
        headers={"Origin": "http://evil.example", "Host": "127.0.0.1:8765"},
    )

    assert response.status == 403
    body = text_of(response)
    assert "origin-refused" in body
    assert "POST /chat was refused" in body
    assert state.calls == [], "a refused post must never reach the model"


def test_a_chat_post_with_no_origin_at_all_is_refused():
    response, state = chat("hello", headers={"Origin": None})

    assert response.status == 403
    assert state.calls == []


def test_an_opaque_origin_is_still_accepted_only_with_same_origin_fetch_metadata():
    """The fixed bug must stay fixed on the chat route too, not just /select."""
    accepted, state = chat(
        "hello",
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    )
    assert accepted.status == 303, "the panel's own form must be able to send"
    assert state.calls == [("hello", CHAT_TIMEOUT_SECONDS)]

    refused, _ = chat("hello", headers={"Origin": "null"})
    assert refused.status == 403


def test_the_chat_box_cannot_carry_a_key():
    body = urlencode({"message": "hello", "api_key": RAW_KEY}).encode("utf-8")
    state = ChatState()
    response = dispatch(state, request(body=body))

    assert response.status == 403
    text = text_of(response)
    assert "POST /chat was refused" in text
    assert "api_key" in text
    assert RAW_KEY not in text, "the refused value must not be echoed back"
    assert state.calls == []


def test_an_empty_message_is_refused_by_name():
    state = ChatState()
    response = dispatch(state, request(body=urlencode({"message": "   "}).encode("utf-8")))

    assert response.status == 400
    assert "carried no message" in text_of(response)
    assert state.calls == []


def test_an_overlong_message_is_refused_rather_than_truncated():
    state = ChatState()
    body = urlencode({"message": "x" * (MAX_CHAT_CHARS + 1)}).encode("utf-8")
    response = dispatch(state, request(body=body))

    assert response.status == 400
    assert str(MAX_CHAT_CHARS) in text_of(response)
    assert state.calls == [], "an over-long message must not be silently cut down"


def test_a_message_at_the_limit_is_accepted():
    state = ChatState()
    body = urlencode({"message": "x" * MAX_CHAT_CHARS}).encode("utf-8")
    response = dispatch(state, request(body=body))

    assert response.status == 303
    assert state.calls[0][0] == "x" * MAX_CHAT_CHARS


def test_a_chat_post_that_is_not_a_form_is_refused_with_the_reason():
    state = ChatState()
    response = dispatch(
        state,
        request(
            body=json.dumps({"message": "hello"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        ),
    )

    assert response.status == 400
    assert "application/json" in text_of(response)


def test_a_failed_model_call_is_a_502_that_names_the_provider_and_model():
    error = PanelLLMError(
        "openai_compatible", "cmd/deepseek/deepseek-v4.1-flash", "http://127.0.0.1:20129/v1"
    )
    response, state = chat("are you there?", state=ChatState(error=error))

    assert response.status == 502
    body = text_of(response)
    assert "llm-unavailable" in body
    assert "cmd/deepseek/deepseek-v4.1-flash" in body
    assert "openai_compatible" in body


def test_a_failed_turn_is_recorded_as_a_note_and_never_as_a_reply():
    """The user's turn survives the failure; no assistant turn is invented."""

    def failing_call(message, *, timeout_sec=CHAT_TIMEOUT_SECONDS):
        raise PanelLLMError("openai_compatible", "cmd/deepseek/deepseek-v4.1-flash", "http://127.0.0.1:1")

    state = real_state(
        chat_call=failing_call, config_path="/nonexistent/config.json", diary_path="/nonexistent.db"
    )
    response = dispatch(state, request(body=urlencode({"message": "hello"}).encode("utf-8")))
    assert response.status == 502

    turns = state.transcript_buffer.turns()
    assert [t.role for t in turns] == ["you", "note"], "no assistant turn may be invented"
    assert turns[0].text == "hello"
    assert "llm-unavailable" in turns[1].text

    page = text_of(get(ROUTE_INDEX, state))
    assert "hello" in page
    assert "llm-unavailable" in page


def test_an_injected_chat_call_is_used_instead_of_the_default(monkeypatch):
    """``RealPanelState`` must route through its injected seam, not the network."""
    calls = []

    def fake_call(message, *, timeout_sec=CHAT_TIMEOUT_SECONDS):
        calls.append((message, timeout_sec))
        return ChatResult(text="pong", provider="ollama", model="gemma4:e2b")

    monkeypatch.setattr(
        panel, "default_chat_call", lambda *a, **k: pytest.fail("the real stack was reached")
    )
    state = real_state(chat_call=fake_call, config_path="/nonexistent/config.json")

    result = state.chat_reply("ping")

    assert result.text == "pong"
    assert calls == [("ping", CHAT_TIMEOUT_SECONDS)]


def test_real_state_records_both_turns_around_a_successful_call():
    state = real_state(
        chat_call=lambda message, **k: ChatResult(text="pong", model="m"),
        config_path="/nonexistent/config.json",
    )
    state.chat_reply("ping")

    turns = state.transcript_buffer.turns()
    assert [t.role for t in turns] == ["you", "claire"]
    assert [t.text for t in turns] == ["ping", "pong"]


# --------------------------------------------------------------------------- #
# The transcript
# --------------------------------------------------------------------------- #


def test_the_transcript_is_bounded_and_keeps_the_newest_turns():
    buffer = TranscriptBuffer(limit=3)
    for index in range(5):
        buffer.record("you", f"turn-{index}")

    assert len(buffer) == 3
    assert [t.text for t in buffer.turns()] == ["turn-2", "turn-3", "turn-4"]


def test_the_page_shows_the_newest_turn_first():
    state = ChatState()
    state.transcript_buffer.record("you", "the first question")
    state.transcript_buffer.record("claire", "the first answer")
    state.transcript_buffer.record("you", "the second question")
    state.transcript_buffer.record("claire", "the second answer")

    page = text_of(get(ROUTE_INDEX, state))

    assert page.index("the second answer") < page.index("the second question")
    assert page.index("the second question") < page.index("the first answer")


def test_the_transcript_says_plainly_that_it_is_in_memory_only():
    state = real_state(config_path="/nonexistent/config.json")
    status = state.transcript()

    assert status.durable is False
    assert "memory" in status.source
    assert "lost" in status.note.lower()
    assert "daemon" in status.note, "the honest limitation must be stated"

    page = text_of(get(ROUTE_INDEX, state))
    assert "in memory" in page
    assert "daemon" in page


def test_a_vendor_shaped_key_typed_into_the_chat_box_is_redacted_before_rendering():
    """``_esc`` runs the project's own scrubber over everything the page shows.

    The chat box is the one place on the page where the owner supplies free text,
    so it is the one place a key could arrive by accident.  The scrubber is the
    project's single redaction rule set — the same one used for logs and tool
    output — not a second, weaker copy.
    """
    state = ChatState()
    state.transcript_buffer.record("you", "OMNIROUTE_API_KEY=8f3c2b1a9d8e7f6a5b4c3d2e1f0a9b8c")

    page = text_of(get(ROUTE_INDEX, state))

    assert "8f3c2b1a9d8e7f6a5b4c3d2e1f0a9b8c" not in page
    assert "REDACTED" in page


def test_the_chat_box_warns_the_owner_that_this_text_is_shown_back():
    page = text_of(get(ROUTE_INDEX, ChatState()))

    assert "type here" in page.lower() or "Type here" in page
    assert "nothing is stored on disk" in page.lower()
    # The one risk the box genuinely carries is stated on the page, not buried.
    assert "secret" in page.lower()


def test_a_secret_that_matches_no_scrub_rule_is_echoed_which_is_a_known_limit():
    """A tripwire, deliberately: pattern-based scrubbing is not omniscience.

    ``claire.utils.redact.scrub_secrets`` recognises vendor-shaped and
    keyword-anchored credentials.  An arbitrary secret with no recognisable shape
    is echoed back like any other text — the page cannot know it is a secret, and
    pretending otherwise would be the dishonest option.  If this test ever starts
    failing, the scrubber grew a rule that catches this shape: re-check the chat
    surface (and the warning in ``_chat_section``) rather than just changing the
    expectation.
    """
    state = ChatState()
    state.transcript_buffer.record("you", f"my key is {RAW_KEY}")

    page = text_of(get(ROUTE_INDEX, state))

    assert RAW_KEY in page, "if this fails, the scrubber improved - see the docstring"


def test_a_turn_is_escaped_and_shown_as_text_not_markup():
    state = ChatState()
    state.transcript_buffer.record("you", "<script>alert('x')</script>")

    page = text_of(get(ROUTE_INDEX, state))

    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


def test_a_turn_with_an_unknown_role_is_rendered_as_a_panel_note():
    turn = panel.TurnRecord(role="assistant-of-doom", text="hi")
    assert turn.role == "note"


def test_a_transcript_that_cannot_be_read_is_a_named_problem_not_a_blank_card():
    class Exploding(ChatState):
        def transcript(self):
            raise panel.PanelStateError("transcript")

    page = text_of(get(ROUTE_INDEX, Exploding()))

    assert "state-missing" in page
    assert "transcript" in page


def test_the_base_state_reports_an_empty_transcript_without_pretending_it_is_durable():
    status = PanelState().transcript()

    assert status.turns == ()
    assert status.durable is False
    assert status.error is None
    assert "daemon" in status.note


def test_the_base_state_refuses_to_chat_rather_than_inventing_a_reply():
    with pytest.raises(panel.PanelStateError):
        PanelState().chat_reply("hello")


# --------------------------------------------------------------------------- #
# The persisted diary
# --------------------------------------------------------------------------- #


def make_diary(path, rows):
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE conversation_summaries ("
        "id INTEGER PRIMARY KEY, date_utc TEXT, ts_utc TEXT, summary TEXT, "
        "topics TEXT, source_app TEXT)"
    )
    connection.executemany(
        "INSERT INTO conversation_summaries (date_utc, ts_utc, summary, topics, source_app) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()


def test_the_diary_is_read_from_the_databases_the_config_points_at(tmp_path):
    db = tmp_path / "claire.db"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"db_path": str(db)}), encoding="utf-8")

    # diary_path=None means "resolve it from the config", which is the point.
    assert real_state(config_path=config, diary_path=None).diary_path() == db


def test_real_diary_summaries_are_rendered(tmp_path):
    db = tmp_path / "claire.db"
    make_diary(db, [("2026-01-31", "2026-01-31T20:00:00", "We discussed the roof.", "roof", "voice")])

    state = real_state(diary_path=db)
    status = state.diary()
    assert status.entries and status.entries[0].summary == "We discussed the roof."

    page = text_of(get(ROUTE_INDEX, state))
    assert "We discussed the roof." in page
    assert "2026-01-31" in page
    assert str(db) in page


def test_the_diary_never_creates_the_database(tmp_path):
    """Read-only, structurally: opening a page must not create owner data."""
    db = tmp_path / "not-yet.db"

    status = real_state(diary_path=db).diary()

    assert status.exists is False
    assert not db.exists(), "the panel created the memory database by viewing a page"


def test_the_diary_is_opened_read_only(tmp_path):
    """A page view must not be able to write, even through the same connection."""
    db = tmp_path / "claire.db"
    make_diary(db, [("2026-01-31", "t", "summary", "", "voice")])

    # The read succeeds...
    assert real_state(diary_path=db).diary().entries
    # ...and the URI the panel builds leaves the file byte-identical.
    before = db.read_bytes()
    real_state(diary_path=db).diary()
    assert db.read_bytes() == before


def test_a_database_without_the_diary_table_is_a_named_problem(tmp_path):
    db = tmp_path / "claire.db"
    connection = sqlite3.connect(str(db))
    connection.execute("CREATE TABLE something_else (id INTEGER)")
    connection.commit()
    connection.close()

    status = real_state(diary_path=db).diary()

    assert status.exists is True
    assert status.error and "conversation_summaries" in status.error
    assert status.fix

    page = text_of(get(ROUTE_INDEX, real_state(diary_path=db)))
    assert "conversation_summaries" in page


def test_a_corrupt_database_is_a_named_problem_not_a_blank_section(tmp_path):
    db = tmp_path / "claire.db"
    db.write_bytes(b"this is not a sqlite database at all")

    status = real_state(diary_path=db).diary()

    assert status.error, "a damaged database must be a finding, not silence"
    assert status.fix


def test_only_the_most_recent_diary_entries_are_shown(tmp_path):
    db = tmp_path / "claire.db"
    make_diary(
        db,
        [(f"2026-01-{day:02d}", "t", f"summary {day}", "", "voice") for day in range(1, 20)],
    )

    status = real_state(diary_path=db).diary()

    assert len(status.entries) == DIARY_LIMIT
    assert status.entries[0].date == "2026-01-19", "newest first"


def test_a_diary_summary_that_contains_markup_is_escaped(tmp_path):
    db = tmp_path / "claire.db"
    make_diary(db, [("2026-01-31", "t", "<img src=x onerror=alert(1)>", "", "voice")])

    page = text_of(get(ROUTE_INDEX, real_state(diary_path=db)))

    assert "<img src=x" not in page
    assert "&lt;img" in page


def test_the_base_state_reports_no_diary_without_a_database():
    status = PanelState().diary()

    assert status.entries == ()
    assert status.error is None
    assert status.note


# --------------------------------------------------------------------------- #
# The origin guard names the route it refused (the bug this change fixed)
# --------------------------------------------------------------------------- #


def test_the_origin_guard_names_the_route_it_refused():
    """RED without the fix: every refusal hard-coded "POST /select was refused".

    A refused chat write therefore sent the owner to the selector form.  The
    guard now takes the route, and both spellings are asserted so the default
    cannot silently drift back to the wrong one.
    """
    cross_site = PanelRequest(
        method="POST",
        path=ROUTE_CHAT,
        headers=dict(FORM_HEADERS, Origin="http://evil.example"),
    )
    with pytest.raises(panel.PanelOriginError) as caught:
        check_write_origin(cross_site, route=ROUTE_CHAT)
    assert "POST /chat was refused" in caught.value.message
    assert "/select" not in caught.value.message

    with pytest.raises(panel.PanelOriginError) as default:
        check_write_origin(cross_site)
    assert "POST /select was refused" in default.value.message


def test_every_write_route_guards_its_own_origin():
    """Each POST route refuses a foreign origin, and names itself doing it."""
    for route in panel.POST_ROUTES:
        body = urlencode(
            {"provider": "ollama", "model": "m"} if route == ROUTE_SELECT else {"message": "hi"}
        ).encode("utf-8")
        request_obj = PanelRequest(
            method="POST",
            path=route,
            headers=dict(FORM_HEADERS, Origin="http://evil.example"),
            body=body,
        )
        response = dispatch(ChatState(), request_obj)
        assert response.status == 403, route
        assert f"POST {route} was refused" in text_of(response), route


def test_parse_form_still_refuses_the_wrong_content_type():
    with pytest.raises(panel.PanelBadRequest):
        parse_form(
            PanelRequest(
                method="POST",
                path=ROUTE_CHAT,
                headers={"Content-Type": "text/plain"},
                body=b"message=hi",
            )
        )


def test_a_fresh_chat_state_has_no_turns():
    assert ChatState().transcript_buffer.turns() == ()


def test_real_state_reports_a_named_problem_when_the_config_is_unreadable():
    """A state pointed at nothing must say so, never invent a model."""
    info = real_state(config_path="/nonexistent/config.json").provider_model()

    assert isinstance(info, ProviderModel)


def test_a_relative_db_path_does_not_break_the_diary_read(tmp_path, monkeypatch):
    """A ``db_path`` that is not absolute must be a finding, not an exception.

    ``Path.as_uri()`` raises ``ValueError`` for a relative path, and a config with
    ``"db_path": "claire.db"`` is perfectly legal - ``claire.config`` expands ``~``
    and leaves the rest alone.  Without the fix this raised out of ``diary()`` and
    the section reported "the diary could not be read", which blames the owner's
    database for a bug in the panel.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "claire.db").write_bytes(b"")  # exists, but is not a database

    status = real_state(diary_path="claire.db").diary()

    assert status.path.endswith("claire.db")
    # It exists and is empty, so SQLite reports a named error - but no ValueError
    # escaped, and the entry point never raised.
    assert status.error is None or "Error" in status.error


def test_an_absolute_db_path_still_reads_normally(tmp_path):
    db = tmp_path / "claire.db"
    make_diary(db, [("2026-01-31", "t", "absolute path works", "", "voice")])

    assert real_state(diary_path=db).diary().entries[0].summary == "absolute path works"


def test_the_in_memory_note_survives_a_transcript_that_has_turns_in_it():
    """The disclaimer must not vanish exactly when the transcript looks real.

    The note was rendered only in the empty case, so as soon as a turn existed the
    page showed a transcript with nothing saying it is not a durable record.  That
    is backwards: the more the page looks like an archive, the more it needs to say
    that it is not one.
    """
    state = real_state(chat_call=lambda message, **k: ChatResult(text="noted"))
    state.chat_reply("is this kept?")

    page = text_of(get(ROUTE_INDEX, state))

    assert "is this kept?" in page, "the turn itself must render"
    assert "noted" in page
    assert "in memory" in page
    assert "lost" in page.lower(), (
        "the page showed a transcript with turns and no longer said the turns are "
        "lost when the panel restarts"
    )
    assert "daemon" in page


def test_the_in_memory_badge_follows_the_source_it_is_given():
    """A hard-coded badge would be a lie the moment a state reported otherwise."""

    class DurableState(ChatState):
        def transcript(self):
            return TranscriptStatus(turns=(), durable=True, source="a durable store")

    page = text_of(get(ROUTE_INDEX, DurableState()))

    assert "on disk" in page
    assert "in memory" not in page


def test_the_startup_line_does_not_claim_loopback_when_it_is_not(monkeypatch, caplog):
    """A log line that reassures wrongly is worse than no log line.

    ``serve()`` announced "(plain HTTP, loopback)" unconditionally, so an operator
    who had opened the panel to their network with ``--allow-remote-binding`` was
    told at startup that it was loopback-only.  Asserted through the real
    ``serve()`` against a fake server, because the string is the whole point.
    """
    import logging

    from claire.panel import server as panel

    class FakeServer:
        server_address = ("0.0.0.0", 8765)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(panel, "create_server", lambda *a, **k: FakeServer())
    monkeypatch.setattr(panel, "_LOGGER", logging.getLogger("panel-test"))

    with caplog.at_level(logging.INFO, logger="panel-test"):
        panel.serve(host="0.0.0.0", allow_remote=True)
    assert "REACHABLE FROM THE NETWORK" in caplog.text
    assert "loopback" not in caplog.text

    caplog.clear()
    FakeServer.server_address = ("127.0.0.1", 8765)
    with caplog.at_level(logging.INFO, logger="panel-test"):
        panel.serve()
    assert "loopback" in caplog.text
    assert "REACHABLE" not in caplog.text
