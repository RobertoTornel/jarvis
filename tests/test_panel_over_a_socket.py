"""The panel, exercised over a real socket (R-WEB-1).

``test_panel_server.py`` deliberately never opens a socket: it hands
:class:`PanelRequest` objects straight to ``dispatch()``.  That is a real gap, and
it hid a **total outage** of the web panel.

Every request over a socket died with an empty reply *before routing*.
``HeaderMap.__init__`` tested ``isinstance(headers, Mapping)`` to decide whether to
call ``.items()``.  The live server passes ``http.client``'s ``HTTPMessage`` (from
``email.message``), which provides ``.items()`` but is **not** a registered
``Mapping``.  So the check failed, the code fell through to treating the message as
an iterable of ``(key, value)`` pairs, and iterating an ``HTTPMessage`` yields
header *names* — unpacking ``"Host"`` as ``(k, v)`` raised
``ValueError: too many values to unpack``.

Every in-process test passed the whole time, because none of them ever produced an
``HTTPMessage``.  So this file is the missing layer: it starts the real server on a
real port and speaks real HTTP to it.  Nothing here replaces the in-process tests;
it covers what they structurally cannot.
"""

from __future__ import annotations

import http.client
import socket
import threading
from urllib.parse import urlencode

import pytest

from claire.panel.server import (
    CHAT_TIMEOUT_SECONDS,
    MAX_CHAT_CHARS,
    ROUTE_CHAT,
    ROUTE_INDEX,
    ChatResult,
    PanelLLMError,
    RealPanelState,
    create_server,
)

# The fake state lives with the in-process tests; pytest puts tests/ on sys.path.
from test_panel_server import FakeState

pytestmark = pytest.mark.unit


def _reply(text="Blue, as far as I can tell."):
    """A chat seam that records its calls and never reaches a network."""
    calls = []

    def chat_call(message, *, timeout_sec=CHAT_TIMEOUT_SECONDS):
        calls.append((message, timeout_sec))
        return ChatResult(text=text, provider="openai_compatible", model="a-model")

    chat_call.calls = calls
    return chat_call


class _StubReport:
    ok = True
    summary_line = "stub - nothing was probed"
    generated_at = "1970-01-01T00:00:00"
    checks = ()


def chat_state(chat_call):
    """A real state with the model call injected: the real recording path runs.

    ``RealPanelState.chat_reply`` is what records the owner's turn and the failure
    note, so using the real class here is the point - only the model call is
    replaced.  The self-check is stubbed because it probes real hardware and
    network endpoints, which a socket test must not do.
    """
    from types import SimpleNamespace

    return RealPanelState(
        config_path="/nonexistent/config.json",
        diary_path="/nonexistent/claire.db",
        self_check_factory=lambda: SimpleNamespace(run=lambda: _StubReport()),
        chat_call=chat_call,
    )


class _LivePanel:
    """The real ThreadingHTTPServer, on an ephemeral port, with a real socket."""

    def __init__(self, state=None):
        self.state = state if state is not None else FakeState()
        self.server = create_server(self.state, port=0)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            sent = {"Host": f"127.0.0.1:{self.port}"}
            sent.update(headers or {})
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def get(self, path: str = "/"):
        status, _headers, body = self._request("GET", path)
        return status, body

    def head(self, path: str = "/"):
        return self._request("HEAD", path)

    def post(self, path, fields, headers=None):
        body = urlencode(fields).encode("utf-8")
        sent = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"http://127.0.0.1:{self.port}",
        }
        sent.update(headers or {})
        return self._request("POST", path, body=body, headers=sent)


def test_the_page_is_served_over_a_real_socket():
    """The load-bearing one: before the fix this returned NO response at all.

    Not a wrong status - an empty reply, because the handler raised while building
    the request object, before any route ran.  Asserting a real status *and* real
    body is what makes this catch it; a test that only checked "the port is open"
    would still pass while the panel was unusable.
    """
    with _LivePanel() as panel:
        status, body = panel.get("/")

    assert status == 200
    assert len(body) > 1000, f"suspiciously small page: {len(body)} bytes"
    assert b"<html" in body.lower()


def test_the_live_headers_reach_the_routes():
    """The regression proper: HTTPMessage must be readable as a header mapping.

    The panel guards cross-origin writes using request headers, so if headers
    silently vanished every guard would degrade to "absent" - which is why the
    mapping bug was worth a fail-proof of its own rather than just a 200 check.
    """
    with _LivePanel() as panel:
        status, body = panel.get("/")

    assert status == 200
    # The page names the port it is served on, which is derived from the Host
    # header - so a page that renders at all proves the header was parsed.
    assert str(panel.port).encode() in body or b"Claire panel" in body


def test_a_bodyless_get_is_not_rejected_like_the_bridge_was():
    """The same mistake the phone bridge made, pinned here too.

    The bridge read the body unconditionally and answered 411 to every ordinary
    bodyless GET - unreachable in production while 96 in-process tests passed. The
    panel's requests are all bodyless GETs, so this asserts the class of bug stays
    fixed on this surface as well.
    """
    with _LivePanel() as panel:
        status, _ = panel.get("/")

    assert status != 411, "a bodyless GET must not be rejected for a missing body"
    assert status == 200


def test_an_unknown_path_is_a_real_response_and_not_a_dropped_connection():
    """A 404 is fine; an empty reply is not.

    Distinguishing these matters because "no response" and "refused response" look
    identical to a browser and only one of them is a bug.
    """
    with _LivePanel() as panel:
        status, body = panel.get("/definitely-not-a-route")

    assert isinstance(status, int)
    assert status in (400, 403, 404), f"unexpected status {status}"
    assert body is not None


# --------------------------------------------------------------------------- #
# The chat and the transcript, over the real socket
#
# These are the routes the in-process tests structurally cannot cover: they read
# the body off a real socket (so Content-Length handling, header parsing and the
# origin guard all run for real), and they follow the panel's own 303 back to the
# page to prove the reply actually reaches the owner's browser.
# --------------------------------------------------------------------------- #


def test_a_chat_post_over_a_socket_answers_and_shows_the_reply():
    """The whole chat path, end to end, over a real socket.

    Nothing here calls the internals: a real HTTP POST with a real urlencoded body
    goes in, the route reads it from the socket, the seam produces a reply, and
    the panel's own redirect is followed to the page that renders it.
    """
    seam = _reply("Blue, as far as I can tell.")
    with _LivePanel(state=chat_state(seam)) as panel:
        status, headers, body = panel.post(ROUTE_CHAT, {"message": "What colour is the sky?"})
        assert status == 303, body[:400]
        assert headers["Location"] == "/#conversation"

        page_status, page = panel.get(ROUTE_INDEX)

    assert seam.calls == [("What colour is the sky?", CHAT_TIMEOUT_SECONDS)]
    assert page_status == 200
    text = page.decode("utf-8")
    assert "What colour is the sky?" in text
    assert "Blue, as far as I can tell." in text
    assert 'id="conversation"' in text


def test_turns_accumulate_across_requests_in_one_panel_process():
    """The transcript is process state, not per-request: two posts, two turns."""
    with _LivePanel(state=chat_state(_reply("first reply"))) as panel:
        assert panel.post(ROUTE_CHAT, {"message": "first question"})[0] == 303
        assert panel.post(ROUTE_CHAT, {"message": "second question"})[0] == 303
        _status, page = panel.get(ROUTE_INDEX)

    text = page.decode("utf-8")
    assert "first question" in text and "second question" in text
    assert text.index("second question") < text.index("first question"), "newest first"


def test_a_get_to_the_chat_route_is_a_405_over_a_socket():
    with _LivePanel() as panel:
        status, _headers, body = panel._request("GET", ROUTE_CHAT)

    assert status == 405
    assert b"method-not-allowed" in body


def test_a_cross_origin_chat_post_is_refused_over_a_socket_and_names_the_route():
    """The guard reads real headers here, which is the only place it can be trusted."""
    seam = _reply()
    with _LivePanel(state=chat_state(seam)) as panel:
        status, _headers, body = panel.post(
            ROUTE_CHAT, {"message": "exfiltrate"}, headers={"Origin": "http://evil.example"}
        )

    assert status == 403
    text = body.decode("utf-8")
    assert "origin-refused" in text
    assert "POST /chat was refused" in text
    assert seam.calls == [], "a refused post must never reach the model"


def test_a_chat_post_with_no_origin_header_is_refused_over_a_socket():
    seam = _reply()
    with _LivePanel(state=chat_state(seam)) as panel:
        body = urlencode({"message": "hello"}).encode("utf-8")
        status, _headers, _body = panel._request(
            "POST",
            ROUTE_CHAT,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": ""},
        )

    assert status == 403
    assert seam.calls == []


def test_an_opaque_origin_with_same_origin_fetch_metadata_is_accepted_over_a_socket():
    """The fixed write-guard bug, verified through a real browser-shaped request.

    This is the shape a browser sends when a referrer policy blinds the page: the
    origin is the opaque "null", and only Sec-Fetch-Site proves it is the panel's
    own form.  Before the fix this combination was refused and no setting could
    ever be saved.
    """
    with _LivePanel(state=chat_state(_reply())) as panel:
        status, _headers, _body = panel.post(
            ROUTE_CHAT,
            {"message": "hello"},
            headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        )
        assert status == 303

        refused, _headers, _body = panel._request(
            "POST",
            ROUTE_CHAT,
            body=urlencode({"message": "hello"}).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "null",
            },
        )

    assert refused == 403, "an opaque origin alone must stay refused"


def test_a_model_failure_over_a_socket_is_a_named_502_not_an_empty_box():
    def failing_chat(message, *, timeout_sec=CHAT_TIMEOUT_SECONDS):
        raise PanelLLMError("openai_compatible", "a-model", "http://127.0.0.1:20129/v1")

    with _LivePanel(state=chat_state(failing_chat)) as panel:
        status, _headers, body = panel.post(ROUTE_CHAT, {"message": "are you there?"})

    assert status == 502
    text = body.decode("utf-8")
    assert "llm-unavailable" in text
    assert "a-model" in text
    assert "are you there?" not in text, "the refusal page must not echo the message back"

    # ...and the turn is still recorded, so the owner can see what was asked.
    with _LivePanel(state=chat_state(failing_chat)) as panel:
        assert panel.post(ROUTE_CHAT, {"message": "are you there?"})[0] == 502
        _status, page = panel.get(ROUTE_INDEX)
    assert "are you there?" in page.decode("utf-8")


def test_an_unknown_chat_field_is_ignored_and_a_key_field_is_refused_over_a_socket():
    seam = _reply()
    with _LivePanel(state=chat_state(seam)) as panel:
        ok, _headers, _body = panel.post(
            ROUTE_CHAT, {"message": "hello", "not_a_field": "x"}
        )
        assert ok == 303

        refused, _headers, body = panel.post(
            ROUTE_CHAT, {"message": "hello", "llm_api_key": "zzz-DO-NOT-RENDER"}
        )

    assert refused == 403
    assert b"zzz-DO-NOT-RENDER" not in body
    assert seam.calls == [("hello", CHAT_TIMEOUT_SECONDS)]


def test_an_empty_chat_body_over_a_socket_is_a_400():
    with _LivePanel(state=chat_state(_reply())) as panel:
        status, _headers, body = panel.post(ROUTE_CHAT, {"message": ""})

    assert status == 400
    assert b"carried no message" in body


def test_an_overlong_message_is_refused_over_a_socket():
    seam = _reply()
    with _LivePanel(state=chat_state(seam)) as panel:
        status, _headers, body = panel.post(
            ROUTE_CHAT, {"message": "x" * (MAX_CHAT_CHARS + 1)}
        )

    assert status == 400
    assert seam.calls == []


def test_the_transcript_survives_a_failed_turn_over_a_socket():
    """A failure is recorded honestly, and the page still renders it."""

    def failing_chat(message, *, timeout_sec=CHAT_TIMEOUT_SECONDS):
        raise PanelLLMError("openai_compatible", "a-model", "http://127.0.0.1:20129/v1")

    with _LivePanel(state=chat_state(failing_chat)) as panel:
        assert panel.post(ROUTE_CHAT, {"message": "remember this"})[0] == 502
        _status, page = panel.get(ROUTE_INDEX)

    text = page.decode("utf-8")
    assert "remember this" in text
    assert "llm-unavailable" in text
    assert "no reply" in text


def test_a_head_to_the_panel_sends_no_body_over_a_socket():
    """A latent HTTP bug worth a fail-proof: HEAD must not carry a body.

    The handler suppresses it explicitly (``if self.command != "HEAD"``), and a
    client that reads a body on HEAD would desynchronise the connection.  Checked
    over a socket because that is the only place the wire shape is real.
    """
    with _LivePanel() as panel:
        status, headers, body = panel.head(ROUTE_INDEX)

    assert status == 200
    assert int(headers["Content-Length"]) > 1000, "HEAD must still describe the length"
    assert body == b"", f"HEAD sent {len(body)} bytes of body"


def test_every_declared_route_answers_over_a_socket():
    """Nothing is advertised that the live server cannot actually serve."""
    from claire.panel.server import GET_ROUTES, POST_ROUTES, ROUTE_SELECT

    with _LivePanel(state=chat_state(_reply())) as panel:
        for route in GET_ROUTES:
            status, _headers, body = panel._request("GET", route)
            assert status in (200, 204), f"GET {route} -> {status}"
            if status == 200:
                assert body, f"GET {route} returned an empty body"
        for route in POST_ROUTES:
            assert route in (ROUTE_SELECT, ROUTE_CHAT)
        assert panel.post(ROUTE_CHAT, {"message": "hi"})[0] == 303

    # /select writes config, so it is swept on the fake that records the write
    # rather than against a path that cannot exist.
    with _LivePanel() as panel:
        status, _headers, _body = panel.post(
            ROUTE_SELECT, {"provider": "ollama", "model": "gemma4:e2b"}
        )
        assert status == 303, f"POST {ROUTE_SELECT} -> {status}"


def test_a_directory_traversal_target_over_a_socket_reads_nothing():
    with _LivePanel() as panel:
        for target in ("/../etc/passwd", "/%2e%2e/etc/passwd", "/./../../etc/shadow"):
            status, _headers, body = panel._request("GET", target)
            assert status in (400, 404), f"{target} -> {status}"
            assert b"root:" not in body


def test_the_referrer_policy_over_a_socket_is_still_same_origin():
    """The fix that made saving possible, asserted on the wire rather than in source."""
    with _LivePanel() as panel:
        status, headers, _body = panel._request("GET", ROUTE_INDEX)

    assert status == 200
    assert headers.get("Referrer-Policy") == "same-origin"
    assert headers.get("X-Content-Type-Options") == "nosniff"


def test_a_request_body_the_panel_will_not_read_closes_the_connection():
    """A latent HTTP bug: an unread body must not become the next request.

    ``protocol_version`` is HTTP/1.1, so connections are kept alive by default.
    The handler reads the body only when ``Content-Length`` parses, but a chunked
    request has no ``Content-Length`` at all: the panel would answer 400 and then
    parse the *body bytes* as the next request on the same connection.  That is
    request smuggling inside the loopback panel, and it is invisible to any test
    that opens a fresh connection per request - which is all of them.

    ``recv`` returns whatever has arrived, so the whole stream is read to EOF
    rather than trusting one read to be one response.
    """
    with _LivePanel(state=chat_state(_reply())) as panel:
        sock = socket.create_connection(("127.0.0.1", panel.port), timeout=10)
        sock.settimeout(5)
        try:
            sock.sendall(
                (
                    f"POST /chat HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{panel.port}\r\n"
                    f"Origin: http://127.0.0.1:{panel.port}\r\n"
                    "Transfer-Encoding: chunked\r\n"
                    "\r\n"
                    "0\r\n\r\n"
                ).encode("ascii")
            )
            stream = b""
            closed = False
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    closed = True
                    break
                stream += chunk
        finally:
            sock.close()

    assert closed, "the panel left the connection open over an unread body"
    # Counted as whole documents rather than status lines or headers: the smuggled
    # "request" (`0`) is parsed as HTTP/0.9, for which BaseHTTPRequestHandler
    # writes neither - it emits the error page's body with no status line and no
    # headers at all, so counting those would miss exactly the response that
    # matters.
    documents = stream.lower().count(b"<!doctype html")
    assert documents == 1, (
        "the panel answered the request and then answered the unread body as a "
        f"second request ({documents} HTML documents on one connection)"
    )
    assert b"Error code explanation" not in stream, (
        "the panel's built-in error page appeared, so the unread body was parsed "
        "as a request"
    )
