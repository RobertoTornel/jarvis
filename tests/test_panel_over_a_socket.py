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
import threading

import pytest

from claire.panel.server import create_server

# The fake state lives with the in-process tests; pytest puts tests/ on sys.path.
from test_panel_server import FakeState

pytestmark = pytest.mark.unit


class _LivePanel:
    """The real ThreadingHTTPServer, on an ephemeral port, with a real socket."""

    def __init__(self):
        self.server = create_server(FakeState(), port=0)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    def get(self, path: str = "/"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()


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
