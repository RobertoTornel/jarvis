"""Tests for :mod:`claire.phone.bridge`.

Spec: ``src/claire/phone/bridge.spec.md`` (R-IO-1, R-LOC-1, R-VISION-1).

Everything here is offline and socket-free.  The whole request path is an
ordinary function call -- ``PhoneBridge.handle(method, path, content_type=...,
body=...)`` -- so no server is started, no thread is needed for the handler and
no request leaves this process.  The key store, the utterance sink, the vision
describer, the reply queue, the location sink and the photo directory are all
injected under ``tmp_path``; nothing touches ``~/.config/claire`` or the real
cache directory.

The suite is built around the five invariants the spec claims:

* **auth is mandatory and not an oracle** -- missing, wrong and same-length-wrong
  tokens are all 401 with byte-identical bodies, and *nothing runs*: no
  utterance reaches the sink, no photo is written, the reply queue is not
  drained;
* **the token is only ever a masked handle** -- it appears in no response body,
  no repr and no captured log line;
* **loopback by default** -- a public bind needs ``allow_public_bind=True`` and
  then warns in plain words; without it the refusal happens before any server
  object exists;
* **no fabrication about photos** -- with no describer configured the reply says
  so and carries no ``description``; the bytes are nevertheless stored and the
  *path* is what the describer receives;
* **named failures** -- 413, 415, 400, 404, 405 and 503 each carry a code, a
  message and a remedy.

Two checks are deliberately fail-proof rather than merely behavioural: an
oversized body must be refused *before the stream is read at all*, and a public
bind must fail *before a socket object is constructed*.
"""

from __future__ import annotations

import base64
import inspect
import contextlib
import http.client
import importlib
import json
import logging
import re
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

from claire.platform.keys import KeyStore, mask
from claire.phone import bridge
from claire.phone.bridge import (
    ALLOWED_IMAGE_CONTENT_TYPES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_BODY_BYTES,
    TOKEN_KEY_NAME,
    AuthenticationError,
    BodyTooLargeError,
    InvalidPayloadError,
    LocationRecord,
    PhoneBridge,
    PhoneRequestHandler,
    PublicBindRefusedError,
    ReplyQueue,
    check_token,
    default_host,
    is_loopback_host,
    is_tailscale_host,
    media_type,
    parse_bearer_token,
    read_request_body,
    resolve_bind_host,
)

# The gate is `pytest tests/ -m unit`. A test file without this marker is
# silently DESELECTED, so it can pass forever without ever running.
pytestmark = pytest.mark.unit

LOG_NAME = bridge.LOG.name

# A deliberately fake photo. It is not a real image and never was: the vision
# describer is injected, so nothing decodes it.
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"phone-photo-bytes" * 16
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"pretend-image" * 16


# --------------------------------------------------------------------------- #
# Fakes and helpers
# --------------------------------------------------------------------------- #

class RecordingSink:
    """An utterance sink that records what reached it, and can be made to fail."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.utterances: list[dict] = []
        self.error = error

    def __call__(self, utterance: dict) -> None:
        if self.error is not None:
            raise self.error
        self.utterances.append(utterance)


class RecordingDescriber:
    """The injected vision collaborator: records (path, question) per call."""

    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[Path, str]] = []

    def describe(self, image_path, question):
        self.calls.append((Path(image_path), question))
        if self.error is not None:
            raise self.error
        if callable(self.result):
            return self.result(image_path, question)
        return self.result


class ExplodingStream:
    """A body stream that fails the test if it is read at all."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, size: int = -1) -> bytes:  # pragma: no cover - must not run
        self.reads += 1
        raise AssertionError("the body was read before the size check")


def image_description_seen(image_path, question):
    """A real :class:`ImageDescription` in the *seen* state, built by the module."""
    from claire.senses.camera import ImageDescription

    return ImageDescription(
        seen=True,
        description="a red bicycle leaning against a wall",
        image_path=str(image_path),
        question=question,
    )


def image_description_not_seen(image_path, question):
    """A real *not seen* result whose error is the named vision-unavailable one."""
    from claire.senses.camera import ImageDescription, VisionUnavailableError

    return ImageDescription.not_seen(
        image_path,
        question,
        VisionUnavailableError("no vision key is configured"),
    )


def make_bridge(
    tmp_path: Path,
    *,
    sink=None,
    describer=None,
    queue=None,
    location_sink=None,
    max_body_bytes: int = MAX_BODY_BYTES,
    photo_dir=None,
    store=None,
    **kwargs,
) -> PhoneBridge:
    """A bridge wired entirely to fakes, with a throwaway 0600 key store."""
    return PhoneBridge(
        store=store if store is not None else KeyStore(tmp_path / "keys.json"),
        sink=sink,
        describer=describer,
        queue=queue,
        location_sink=location_sink,
        photo_dir=photo_dir if photo_dir is not None else tmp_path / "photos",
        max_body_bytes=max_body_bytes,
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        env={},
        clock=lambda: 1_700_000_000.0,
        **kwargs,
    )


def auth(bridge_obj: PhoneBridge) -> dict:
    """The one header a well-behaved phone sends."""
    return {"Authorization": f"Bearer {bridge_obj.token()}"}


def post_json(bridge_obj: PhoneBridge, path: str, payload: dict, headers=None):
    merged = auth(bridge_obj)
    merged.update(headers or {})
    return bridge_obj.handle(
        "POST",
        path,
        content_type="application/json",
        body=json.dumps(payload).encode("utf-8"),
        headers=merged,
    )


def error_code(response) -> str:
    return response.payload["error"]["code"]


# --------------------------------------------------------------------------- #
# A missing token: 401, and nothing at all happened
# --------------------------------------------------------------------------- #

def test_no_token_is_401_and_the_utterance_sink_never_ran(tmp_path):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink)

    response = b.handle(
        "POST",
        "/text",
        content_type="application/json",
        body=json.dumps({"text": "turn on the kitchen light"}).encode("utf-8"),
        headers={},
    )

    assert response.status == 401
    assert response.ok is False
    assert error_code(response) == "unauthorized"
    assert sink.utterances == [], "an unauthenticated request reached the sink"


def test_no_token_does_not_drain_the_reply_queue(tmp_path):
    queue = ReplyQueue()
    queue.put_text("the kettle is boiling")
    b = make_bridge(tmp_path, queue=queue)

    response = b.handle("GET", "/reply", headers={})

    assert response.status == 401
    assert len(queue) == 1, "an unauthenticated request consumed Claire's reply"


def test_no_token_does_not_store_a_photo(tmp_path):
    describer = RecordingDescriber(image_description_seen)
    b = make_bridge(tmp_path, describer=describer)

    response = b.handle(
        "POST",
        "/see",
        content_type="image/jpeg",
        body=FAKE_JPEG,
        headers={},
    )

    assert response.status == 401
    assert describer.calls == []
    assert not (tmp_path / "photos").exists(), "a photo was stored without a token"


def test_no_token_is_401_even_on_health(tmp_path):
    b = make_bridge(tmp_path)

    assert b.handle("GET", "/health", headers={}).status == 401


# --------------------------------------------------------------------------- #
# Wrong tokens, including one of exactly the same length
# --------------------------------------------------------------------------- #

def test_a_wrong_token_is_401_and_nothing_runs(tmp_path):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink)

    response = b.handle(
        "POST",
        "/text",
        content_type="application/json",
        body=json.dumps({"text": "hello"}).encode("utf-8"),
        headers={"Authorization": "Bearer definitely-not-the-token"},
    )

    assert response.status == 401
    assert error_code(response) == "unauthorized"
    assert sink.utterances == []


def test_a_wrong_token_of_the_same_length_is_401(tmp_path):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink)
    real = b.token()
    same_length = "z" * len(real)
    assert len(same_length) == len(real), "the precondition of this test is length equality"

    response = b.handle(
        "POST",
        "/text",
        content_type="application/json",
        body=json.dumps({"text": "hello"}).encode("utf-8"),
        headers={"Authorization": f"Bearer {same_length}"},
    )

    assert response.status == 401
    assert sink.utterances == []


def test_missing_and_wrong_tokens_get_byte_identical_responses(tmp_path):
    b = make_bridge(tmp_path)
    body = json.dumps({"text": "hello"}).encode("utf-8")

    missing = b.handle("POST", "/text", content_type="application/json", body=body, headers={})
    wrong = b.handle(
        "POST",
        "/text",
        content_type="application/json",
        body=body,
        headers={"Authorization": "Bearer nope"},
    )

    assert missing.status == wrong.status == 401
    assert missing.body() == wrong.body(), "the 401 body distinguishes missing from wrong"
    assert wrong.headers.get("WWW-Authenticate") == "Bearer"


def test_a_401_never_carries_the_offered_token_back(tmp_path):
    b = make_bridge(tmp_path)
    offered = "offered-token-that-must-not-come-back"

    response = b.handle("GET", "/health", headers={"Authorization": f"Bearer {offered}"})

    assert offered.encode() not in response.body()
    assert offered not in json.dumps(response.payload)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "bearer",
        "Token abc",
    ],
)
def test_a_header_that_offers_no_bearer_token_is_refused(tmp_path, header):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink)

    response = b.handle(
        "POST",
        "/text",
        content_type="application/json",
        body=json.dumps({"text": "hello"}).encode("utf-8"),
        headers={"Authorization": header},
    )

    assert response.status == 401
    assert sink.utterances == []


def test_the_bearer_scheme_is_case_insensitive(tmp_path):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink)

    response = b.handle(
        "POST",
        "/text",
        content_type="application/json",
        body=json.dumps({"text": "hello"}).encode("utf-8"),
        headers={"Authorization": f"bearer {b.token()}"},
    )

    assert response.status == 202
    assert len(sink.utterances) == 1


# --------------------------------------------------------------------------- #
# The correct token: the utterance reaches the injected sink
# --------------------------------------------------------------------------- #

def test_the_correct_token_delivers_the_utterance_to_the_injected_sink(tmp_path):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink)

    response = post_json(b, "/text", {"text": "what is the weather like today"})

    assert response.status == 202
    assert response.payload["ok"] is True
    assert response.payload["accepted"] is True
    assert len(sink.utterances) == 1
    utterance = sink.utterances[0]
    assert utterance["kind"] == "text"
    assert utterance["text"] == "what is the weather like today"
    assert utterance["source"] == "phone"
    assert utterance["received_at"] == 1_700_000_000.0


def test_a_failing_sink_is_a_named_error_not_a_false_acceptance(tmp_path):
    sink = RecordingSink(error=RuntimeError("the audio queue is closed"))
    b = make_bridge(tmp_path, sink=sink)

    response = post_json(b, "/text", {"text": "hello"})

    assert response.status == 500
    assert error_code(response) == "sink_failed"
    assert "accepted" not in response.payload
    assert "remedy" in response.payload["error"]


def test_text_with_no_wired_sink_says_so_rather_than_dropping_it(tmp_path):
    b = make_bridge(tmp_path, sink=None)

    response = post_json(b, "/text", {"text": "hello"})

    assert response.status == 503
    assert error_code(response) == "sink_unconfigured"
    assert "PhoneBridge(sink=" in response.payload["error"]["remedy"]


def test_empty_text_is_refused_by_name(tmp_path):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink)

    response = post_json(b, "/text", {"text": "   "})

    assert response.status == 400
    assert error_code(response) == "invalid_payload"
    assert sink.utterances == []


# --------------------------------------------------------------------------- #
# Oversized bodies: a named 413, refused before being read
# --------------------------------------------------------------------------- #

def test_an_oversized_body_is_413_and_never_reaches_the_sink(tmp_path):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink, max_body_bytes=1024)
    huge = json.dumps({"text": "x" * 4096}).encode("utf-8")

    response = b.handle(
        "POST", "/text", content_type="application/json", body=huge, headers=auth(b)
    )

    assert response.status == 413
    assert error_code(response) == "body_too_large"
    assert sink.utterances == []


def test_a_declared_oversized_body_is_refused_before_the_stream_is_read():
    stream = ExplodingStream()

    with pytest.raises(BodyTooLargeError) as excinfo:
        read_request_body(str(MAX_BODY_BYTES + 1), stream, MAX_BODY_BYTES)

    assert stream.reads == 0, "an oversized body was read before being refused"
    assert excinfo.value.code == "body_too_large"
    assert "resize" in excinfo.value.remedy


def test_a_body_with_no_length_header_is_a_named_411():
    with pytest.raises(bridge.LengthRequiredError) as excinfo:
        read_request_body(None, ExplodingStream(), MAX_BODY_BYTES)

    assert excinfo.value.code == "length_required"


def test_a_negative_content_length_is_a_named_400():
    with pytest.raises(InvalidPayloadError) as excinfo:
        read_request_body("-5", ExplodingStream(), MAX_BODY_BYTES)

    assert excinfo.value.code == "invalid_payload"


def test_authorize_raises_the_named_authentication_error(tmp_path):
    b = make_bridge(tmp_path)

    with pytest.raises(AuthenticationError) as excinfo:
        b.authorize({})

    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.status == 401
    assert excinfo.value.remedy


def test_the_default_body_limit_is_megabytes_not_gigabytes():
    assert 1024 * 1024 <= MAX_BODY_BYTES <= 64 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Unknown content types are refused, never guessed at
# --------------------------------------------------------------------------- #

def test_an_unknown_content_type_is_refused_on_text(tmp_path):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink)

    response = b.handle(
        "POST",
        "/text",
        content_type="text/plain",
        body=b"hello",
        headers=auth(b),
    )

    assert response.status == 415
    assert error_code(response) == "unsupported_content_type"
    assert "application/json" in response.payload["error"]["remedy"]
    assert sink.utterances == []


def test_an_unknown_content_type_is_refused_on_see_and_writes_nothing(tmp_path):
    describer = RecordingDescriber(image_description_seen)
    b = make_bridge(tmp_path, describer=describer)

    response = b.handle(
        "POST",
        "/see",
        content_type="application/xml",
        body=b"<photo/>",
        headers=auth(b),
    )

    assert response.status == 415
    assert error_code(response) == "unsupported_content_type"
    assert describer.calls == []
    assert not (tmp_path / "photos").exists()


def test_a_missing_content_type_is_refused_rather_than_assumed(tmp_path):
    b = make_bridge(tmp_path, sink=RecordingSink())

    response = b.handle("POST", "/text", content_type="", body=b"{}", headers=auth(b))

    assert response.status == 415
    assert error_code(response) == "unsupported_content_type"


def test_media_type_strips_parameters_and_is_case_insensitive():
    assert media_type("Application/JSON; charset=utf-8") == "application/json"
    assert media_type("") == ""
    assert media_type(None) == ""


def test_sanitise_for_log_removes_control_characters_and_bounds_length():
    assert bridge.sanitise_for_log("plain") == "plain"
    assert bridge.sanitise_for_log("a\nb\r\tc") == "a?b??c"
    assert bridge.sanitise_for_log("x" * 500) == "x" * bridge.LOG_MAX_CHARS + "…"
    assert "\x00" not in bridge.sanitise_for_log("a\x00b")


# --------------------------------------------------------------------------- #
# POST /see: the photo is stored and its path handed to the describer
# --------------------------------------------------------------------------- #

def test_see_stores_the_image_and_passes_the_path_to_the_describer(tmp_path):
    describer = RecordingDescriber(image_description_seen)
    b = make_bridge(tmp_path, describer=describer)

    response = b.handle(
        "POST",
        "/see",
        content_type="image/jpeg",
        body=FAKE_JPEG,
        headers={**auth(b), bridge.QUESTION_HEADER: "what is this?"},
    )

    assert response.status == 200
    assert response.payload["ok"] is True
    assert response.payload["seen"] is True
    assert response.payload["description"] == "a red bicycle leaning against a wall"

    assert len(describer.calls) == 1
    stored_path, question = describer.calls[0]
    assert question == "what is this?"
    assert stored_path.name.startswith("photo-")
    assert stored_path.suffix == ".jpg"
    assert stored_path.parent == tmp_path / "photos"
    assert stored_path.read_bytes() == FAKE_JPEG
    assert stat.S_IMODE(stored_path.stat().st_mode) == 0o600
    assert response.payload["image_path"] == str(stored_path)


def test_see_accepts_the_question_from_the_query_string(tmp_path):
    describer = RecordingDescriber(image_description_seen)
    b = make_bridge(tmp_path, describer=describer)

    response = b.handle(
        "POST",
        "/see?question=is+my+form+right",
        content_type="image/png",
        body=FAKE_PNG,
        headers=auth(b),
        query={"question": ["is my form right"]},
    )

    assert response.status == 200
    assert describer.calls[0][1] == "is my form right"
    assert describer.calls[0][0].suffix == ".png"


def test_see_accepts_a_base64_photo_in_json(tmp_path):
    describer = RecordingDescriber(image_description_seen)
    b = make_bridge(tmp_path, describer=describer)

    response = post_json(
        b,
        "/see",
        {
            "image_base64": base64.b64encode(FAKE_JPEG).decode("ascii"),
            "question": "what is on the desk?",
            "content_type": "image/jpeg",
        },
    )

    assert response.status == 200
    assert response.payload["seen"] is True
    stored_path, question = describer.calls[0]
    assert question == "what is on the desk?"
    assert stored_path.read_bytes() == FAKE_JPEG


def test_invalid_base64_is_a_named_400(tmp_path):
    describer = RecordingDescriber(image_description_seen)
    b = make_bridge(tmp_path, describer=describer)

    response = post_json(b, "/see", {"image_base64": "not base64!!"})

    assert response.status == 400
    assert error_code(response) == "invalid_payload"
    assert describer.calls == []


def test_an_empty_photo_is_a_named_400(tmp_path):
    b = make_bridge(tmp_path, describer=RecordingDescriber(image_description_seen))

    response = b.handle(
        "POST", "/see", content_type="image/jpeg", body=b"", headers=auth(b)
    )

    assert response.status == 400
    assert error_code(response) == "invalid_payload"


# --------------------------------------------------------------------------- #
# POST /see with no describer: it says so instead of describing anything
# --------------------------------------------------------------------------- #

def test_with_no_describer_the_response_says_so_and_describes_nothing(tmp_path):
    b = make_bridge(tmp_path, describer=None)

    response = b.handle(
        "POST", "/see", content_type="image/jpeg", body=FAKE_JPEG, headers=auth(b)
    )

    assert response.status == 503
    assert response.payload["ok"] is False
    assert response.payload["seen"] is False
    assert "description" not in response.payload
    assert "description" not in response.body().decode("utf-8")
    assert error_code(response) == "vision_not_configured"
    assert "not described" in response.payload["error"]["message"]
    # Honest about what *did* happen: the photo is on disk.
    assert Path(response.payload["image_path"]).read_bytes() == FAKE_JPEG


def test_a_describer_reporting_not_seen_never_becomes_a_description(tmp_path):
    describer = RecordingDescriber(image_description_not_seen)
    b = make_bridge(tmp_path, describer=describer)

    response = b.handle(
        "POST", "/see", content_type="image/jpeg", body=FAKE_JPEG, headers=auth(b)
    )

    assert response.payload["seen"] is False
    assert "description" not in response.payload
    assert error_code(response) == "vision_not_configured"
    assert response.status == 503
    assert "OMNIROUTE_API_KEY" in response.payload["error"]["remedy"]


def test_a_raising_describer_is_a_named_failure_with_no_description(tmp_path):
    describer = RecordingDescriber(error=RuntimeError("the router hung up"))
    b = make_bridge(tmp_path, describer=describer)

    response = b.handle(
        "POST", "/see", content_type="image/jpeg", body=FAKE_JPEG, headers=auth(b)
    )

    assert response.status == 502
    assert response.payload["seen"] is False
    assert "description" not in response.payload
    assert error_code(response) == "vision_describer_failed"
    assert "router hung up" in response.payload["error"]["message"]


def test_a_describer_with_the_wrong_contract_is_a_named_failure(tmp_path):
    b = make_bridge(tmp_path, describer=RecordingDescriber(result=1234))

    response = b.handle(
        "POST", "/see", content_type="image/jpeg", body=FAKE_JPEG, headers=auth(b)
    )

    assert response.payload["seen"] is False
    assert "description" not in response.payload
    assert error_code(response) == "vision_describer_contract"


def test_an_unusable_describer_object_is_reported_by_name(tmp_path):
    b = make_bridge(tmp_path, describer=object())

    response = b.handle(
        "POST", "/see", content_type="image/jpeg", body=FAKE_JPEG, headers=auth(b)
    )

    assert response.status == 500
    assert error_code(response) == "vision_describer_unusable"


def test_a_plain_callable_describer_is_accepted(tmp_path):
    calls = []

    def describer(image_path, question):
        calls.append((Path(image_path), question))
        return "a handwritten note"

    b = make_bridge(tmp_path, describer=describer)

    response = b.handle(
        "POST", "/see", content_type="image/jpeg", body=FAKE_JPEG, headers=auth(b)
    )

    assert response.status == 200
    assert response.payload["description"] == "a handwritten note"
    assert calls[0][0].read_bytes() == FAKE_JPEG


def test_the_default_describer_is_the_project_vision_seam(tmp_path):
    b = make_bridge(tmp_path)
    b._describer = bridge.DEFAULT_DESCRIBER  # the production default, not a fake

    resolved = b._resolve_describer()

    from claire.senses.camera import describe_image

    assert resolved is describe_image
    assert b.vision_status() == "project_default"


# --------------------------------------------------------------------------- #
# POST /loc
# --------------------------------------------------------------------------- #

def test_loc_records_the_coordinates(tmp_path):
    recorded: list[LocationRecord] = []
    b = make_bridge(tmp_path, location_sink=recorded.append)

    response = post_json(
        b, "/loc", {"latitude": 51.5074, "longitude": -0.1278, "accuracy": 12.5}
    )

    assert response.status == 200
    assert response.payload["ok"] is True
    # The fix is acknowledged, not read back: the phone is not told its own
    # coordinates, the sink (Claire's context) is where the position goes.
    assert response.payload["recorded"] is True
    assert response.payload["accuracy_m"] == 12.5
    assert "latitude" not in json.dumps(response.payload)
    assert "51.5074" not in json.dumps(response.payload)
    assert "-0.1278" not in json.dumps(response.payload)

    assert len(recorded) == 1
    assert recorded[0].latitude == 51.5074
    assert recorded[0].longitude == -0.1278
    assert recorded[0].received_at == 1_700_000_000.0
    assert b.last_location == recorded[0]


def test_loc_records_without_a_sink_too(tmp_path):
    b = make_bridge(tmp_path)

    response = post_json(b, "/loc", {"lat": 40.0, "lon": -3.0})

    assert response.status == 200
    assert b.last_location is not None
    assert b.last_location.latitude == 40.0
    assert b.last_location.longitude == -3.0


@pytest.mark.parametrize(
    "payload",
    [
        {"latitude": 91.0, "longitude": 0.0},
        {"latitude": -90.5, "longitude": 0.0},
        {"latitude": 0.0, "longitude": 181.0},
        {"latitude": 0.0},
        {"longitude": 0.0},
        {"latitude": "51.5", "longitude": 0.0},
        {"latitude": True, "longitude": 0.0},
        {"latitude": 0.0, "longitude": 0.0, "accuracy": -1.0},
        {"latitude": 0.0, "longitude": 0.0, "accuracy": "close"},
    ],
)
def test_bad_coordinates_are_named_400s_and_record_nothing(tmp_path, payload):
    b = make_bridge(tmp_path)

    response = post_json(b, "/loc", payload)

    assert response.status == 400
    assert error_code(response) == "invalid_payload"
    assert b.last_location is None


def test_a_failing_location_sink_is_named(tmp_path):
    def sink(record):
        raise RuntimeError("the diary is read-only")

    b = make_bridge(tmp_path, location_sink=sink)

    response = post_json(b, "/loc", {"latitude": 1.0, "longitude": 2.0})

    assert response.status == 500
    assert error_code(response) == "location_sink_failed"


# --------------------------------------------------------------------------- #
# GET /reply: once, then empty
# --------------------------------------------------------------------------- #

def test_reply_returns_queued_items_once_and_then_empty(tmp_path):
    queue = ReplyQueue()
    b = make_bridge(tmp_path, queue=queue)
    b.push_reply("the kettle is boiling")
    b.push_reply("also, your mother called")

    first = b.handle("GET", "/reply", headers=auth(b))

    assert first.status == 200
    assert first.payload["count"] == 2
    assert [item["text"] for item in first.payload["items"]] == [
        "the kettle is boiling",
        "also, your mother called",
    ]

    second = b.handle("GET", "/reply", headers=auth(b))
    third = b.handle("GET", "/reply", headers=auth(b))

    assert second.payload["items"] == []
    assert second.payload["count"] == 0
    assert third.payload["items"] == [], "the same reply came back forever"
    assert len(queue) == 0


def test_the_reply_queue_is_guarded_by_a_lock():
    queue = ReplyQueue()
    assert isinstance(queue.lock, type(threading.RLock()))


def test_the_reply_queue_loses_nothing_and_duplicates_nothing_under_threads():
    queue = ReplyQueue()
    per_thread = 250
    writers = 4

    def write(base: int) -> None:
        for index in range(per_thread):
            queue.put(f"item-{base}-{index}")

    drained: list[dict] = []
    stop = threading.Event()

    def drainer() -> None:
        while not stop.is_set():
            drained.extend(queue.drain())
            time.sleep(0)

    drain_thread = threading.Thread(target=drainer)
    threads = [threading.Thread(target=write, args=(base,)) for base in range(writers)]
    drain_thread.start()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stop.set()
    drain_thread.join()
    drained.extend(queue.drain())

    assert sorted(item["text"] for item in drained) == sorted(
        f"item-{base}-{index}" for base in range(writers) for index in range(per_thread)
    )
    assert len(queue) == 0


# --------------------------------------------------------------------------- #
# GET /health
# --------------------------------------------------------------------------- #

def test_health_answers_and_reports_the_masked_token_only(tmp_path):
    b = make_bridge(tmp_path, sink=RecordingSink())

    response = b.handle("GET", "/health", headers=auth(b))

    assert response.status == 200
    assert response.payload["ok"] is True
    assert response.payload["status"] == "ok"
    assert response.payload["host"] == DEFAULT_HOST
    assert response.payload["loopback"] is True
    assert response.payload["token"] == mask(b.token())
    assert response.payload["token"] != b.token()
    assert b.token().encode() not in response.body()
    assert response.payload["max_body_bytes"] == MAX_BODY_BYTES
    # The five canonical endpoints, the audio route the phone needs, the
    # channel back to the phone (`/say`), and the two spellings the shipped 0.2
    # client calls.  Pinned as an exact set on purpose: a route added to
    # ENDPOINTS is an authenticated attack surface, and this is where that has
    # to be acknowledged.
    assert set(response.payload["endpoints"]) == {
        "/text",
        "/say",
        "/transcribe",
        "/see",
        "/loc",
        "/location",
        "/reply",
        "/where",
        "/health",
    }
    assert response.payload["location"] is None


def test_health_reports_the_vision_verdict_honestly(tmp_path):
    without = make_bridge(tmp_path, describer=None)
    with_fake = make_bridge(tmp_path, describer=RecordingDescriber("x"))

    assert without.handle("GET", "/health", headers=auth(without)).payload["vision"] == (
        "not_configured"
    )
    assert with_fake.handle("GET", "/health", headers=auth(with_fake)).payload["vision"] == (
        "configured"
    )


# --------------------------------------------------------------------------- #
# Routing: named 404 and 405
# --------------------------------------------------------------------------- #

def test_an_unknown_route_is_a_named_404(tmp_path):
    b = make_bridge(tmp_path)

    response = b.handle("GET", "/nope", headers=auth(b))

    assert response.status == 404
    assert error_code(response) == "not_found"
    assert "POST /text" in response.payload["error"]["remedy"]


def test_a_wrong_method_is_a_named_405_with_an_allow_header(tmp_path):
    b = make_bridge(tmp_path)

    response = b.handle("GET", "/text", headers=auth(b))

    assert response.status == 405
    assert error_code(response) == "method_not_allowed"
    assert response.headers["Allow"] == "POST"


# --------------------------------------------------------------------------- #
# Binding: loopback by default, public only by explicit opt-in
# --------------------------------------------------------------------------- #

def test_the_default_bind_host_is_loopback(tmp_path, monkeypatch):
    monkeypatch.delenv(bridge.HOST_ENV, raising=False)

    assert DEFAULT_HOST == "127.0.0.1"
    assert is_loopback_host(DEFAULT_HOST)
    assert default_host() == DEFAULT_HOST

    unconfigured = PhoneBridge(store=KeyStore(tmp_path / "keys.json"), describer=None)
    assert unconfigured.host == DEFAULT_HOST
    assert is_loopback_host(unconfigured.host)


@pytest.mark.parametrize(
    "host, expected",
    [
        ("127.0.0.1", True),
        ("127.0.0.53", True),
        ("localhost", True),
        ("LocalHost", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("192.168.1.20", False),
        ("10.0.0.5", False),
        ("100.64.0.1", False),
        ("100.127.255.254", False),
        ("100.128.0.1", False),
        ("example.com", False),
        ("", False),
        (None, False),
        ("999.1.1.1", False),
    ],
)
def test_only_real_loopback_addresses_count_as_loopback(host, expected):
    assert is_loopback_host(host) is expected


@pytest.mark.parametrize("host", ["100.64.0.1", "100.104.1.69", "100.127.255.254"])
def test_tailscale_cgnat_binds_are_silent_and_need_no_opt_in(host, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=LOG_NAME)
    monkeypatch.setattr(bridge, "_tailscale_interface_has_address", lambda address: False)

    assert resolve_bind_host(host, 8765) == host
    assert caplog.text == ""


def test_an_address_owned_by_tailscale0_is_silent_and_needs_no_opt_in(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=LOG_NAME)
    monkeypatch.setattr(bridge, "_tailscale_interface_has_address", lambda address: str(address) == "192.0.2.42")

    assert is_tailscale_host("192.0.2.42")
    assert resolve_bind_host("192.0.2.42", 8765) == "192.0.2.42"
    assert caplog.text == ""


def test_a_public_bind_is_refused_without_the_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_tailscale_interface_has_address", lambda address: False)
    with pytest.raises(PublicBindRefusedError) as excinfo:
        resolve_bind_host("0.0.0.0", 8765)

    error = excinfo.value
    assert error.code == "public_bind_refused"
    assert "allow_public_bind=True" in error.remedy
    assert "unencrypted" in error.remedy
    assert "tunnel" in error.remedy


def test_a_public_bind_refusal_never_constructs_a_server(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_tailscale_interface_has_address", lambda address: False)

    def bomb(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("a server (and so a socket) was created despite the refusal")

    monkeypatch.setattr(bridge, "ThreadingHTTPServer", bomb)
    b = make_bridge(tmp_path)

    with pytest.raises(PublicBindRefusedError):
        bridge.create_server(b, host="0.0.0.0", port=8765)


def test_an_opted_in_public_bind_warns_loudly_in_plain_words(caplog, monkeypatch):
    monkeypatch.setattr(bridge, "_tailscale_interface_has_address", lambda address: False)
    caplog.set_level(logging.WARNING, logger=LOG_NAME)

    assert resolve_bind_host("0.0.0.0", 8765, allow_public_bind=True) == "0.0.0.0"

    assert caplog.text, "the opt-in produced no warning at all"
    lowered = caplog.text.lower()
    assert "unencrypted" in lowered
    assert "plain http" in lowered
    assert "tunnel" in lowered
    assert "allow_public_bind=true" in lowered


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_a_loopback_bind_needs_no_opt_in_and_says_nothing(host, caplog):
    caplog.set_level(logging.WARNING, logger=LOG_NAME)

    assert resolve_bind_host(host, 8765) == host
    assert caplog.text == ""


# --------------------------------------------------------------------------- #
# The token: generated into the key store, and never spoken raw
# --------------------------------------------------------------------------- #

def test_the_token_is_generated_once_and_reused(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    b = make_bridge(tmp_path, store=store)

    first = b.token()
    second = b.token()
    fresh = PhoneBridge(store=store, describer=None, env={})

    assert first == second
    assert TOKEN_KEY_NAME in store
    assert fresh.token() == first, "a second bridge minted a different token"
    assert len(first) >= 16


def test_a_weak_stored_token_is_refused_by_name(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.store(TOKEN_KEY_NAME, "short")
    b = make_bridge(tmp_path, store=store)

    with pytest.raises(bridge.TokenUnavailableError) as excinfo:
        b.token()

    assert excinfo.value.code == "token_unavailable"
    assert "too weak" in str(excinfo.value)


def test_a_broken_key_store_does_not_authenticate_anybody(tmp_path, monkeypatch):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink)

    def broken_get(name, **kwargs):
        raise OSError("the key store is unreadable")

    monkeypatch.setattr(b.store, "get", broken_get)

    response = b.handle(
        "POST",
        "/text",
        content_type="application/json",
        body=json.dumps({"text": "hello"}).encode("utf-8"),
        headers={"Authorization": "Bearer anything-at-all"},
    )

    # An unreadable store cannot be compared against, so the request is refused
    # with a named error and nothing is processed.
    assert response.status == 500
    assert error_code(response) == "token_unavailable"
    assert "keys list" in response.payload["error"]["remedy"]
    assert sink.utterances == []


def test_an_unexpected_failure_is_named_and_logged_not_silent(tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger=LOG_NAME)

    class ExplodingQueue(ReplyQueue):
        def drain(self):
            raise RuntimeError("the queue backend vanished")

    b = make_bridge(tmp_path, queue=ExplodingQueue())

    response = b.handle("GET", "/reply", headers=auth(b))

    assert response.status == 500
    assert error_code(response) == "internal_error"
    assert "the queue backend vanished" in response.payload["error"]["message"]
    assert "report" in response.payload["error"]["remedy"].lower()
    assert "RuntimeError" in caplog.text, "the traceback was not logged"


def test_check_token_is_constant_time_and_refuses_an_empty_stored_token():
    assert check_token("a-token", "a-token") is True
    assert check_token("a-token", "a-tokeX") is False
    assert check_token("a-token", "a-token-longer") is False
    assert check_token("", "") is False
    assert check_token(None, None) is False
    assert check_token("", "anything") is False


def test_check_token_compares_with_hmac_not_equality():
    # The docstring explains the rule and therefore quotes the operator; only
    # the executable body is checked, which is where a leak would live.
    source = inspect.getsource(check_token).replace(check_token.__doc__ or "", "")

    assert "hmac.compare_digest" in source
    assert "==" not in source, "a token comparison used == (a timing oracle)"
    assert "!=" not in source, "a token comparison used != (a timing oracle)"


def test_parse_bearer_token_only_accepts_the_bearer_scheme():
    assert parse_bearer_token("Bearer abc123") == "abc123"
    assert parse_bearer_token("bearer abc123") == "abc123"
    assert parse_bearer_token("Bearer   abc123  ") == "abc123"
    assert parse_bearer_token("Basic abc123") is None
    assert parse_bearer_token("abc123") is None
    assert parse_bearer_token("Bearer") is None
    assert parse_bearer_token("Bearer ") is None
    assert parse_bearer_token(None) is None
    assert parse_bearer_token("") is None


def test_the_token_never_appears_in_a_response_body_a_repr_or_a_log_line(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=LOG_NAME)
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink, describer=RecordingDescriber("a wall"))
    token = b.token()

    responses = [
        b.handle("GET", "/health", headers=auth(b)),
        post_json(b, "/text", {"text": "hello"}),
        post_json(b, "/loc", {"latitude": 1.0, "longitude": 2.0}),
        b.handle("GET", "/reply", headers=auth(b)),
        b.handle("POST", "/see", content_type="image/jpeg", body=FAKE_JPEG, headers=auth(b)),
        b.handle("GET", "/health", headers={"Authorization": f"Bearer {token}wrong"}),
        b.handle("GET", "/health", headers={}),
        b.handle("GET", "/nope", headers=auth(b)),
    ]

    for response in responses:
        rendered = response.body().decode("utf-8")
        assert token not in rendered, "the token leaked into a response body"
        assert token not in json.dumps(response.payload)

    assert token not in repr(b)
    assert token not in repr(b.queue)

    logged = caplog.text
    assert logged, "the bridge logged nothing, so this leak check is vacuous"
    assert token not in logged, "the token leaked into a log line"
    assert mask(token) in logged, "the sanctioned masked handle is what should appear"


def test_a_token_smuggled_into_a_request_line_is_redacted_before_logging(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=LOG_NAME)
    b = make_bridge(tmp_path)
    token = b.token()

    b.handle("GET", f"/health?token={token}", headers=auth(b))

    # The bridge redacts whatever it is asked to log; the raw value never lands.
    assert token not in b.redact(f"GET /health?token={token} HTTP/1.0")
    assert mask(token) in b.redact(f"GET /health?token={token} HTTP/1.0")
    # And the request line the bridge actually logs drops the query entirely.
    assert token not in caplog.text


def test_a_control_character_in_a_path_cannot_forge_a_log_line(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=LOG_NAME)
    b = make_bridge(tmp_path)

    response = b.handle(
        "GET",
        "/health\nphone bridge token ready (\u2026abcd:deadbeef)",
        headers=auth(b),
    )

    assert response.status == 404
    entries = [record.getMessage() for record in caplog.records]
    assert entries, "the request was not logged at all"
    for entry in entries:
        assert "\n" not in entry, "a path newline forged a second log entry"
    assert all("\n" not in line for line in caplog.text.splitlines())


# --------------------------------------------------------------------------- #
# Injection: handler and bridge take their collaborators, and no socket is used
# --------------------------------------------------------------------------- #

def test_the_bridge_takes_every_collaborator_by_constructor():
    parameters = inspect.signature(PhoneBridge.__init__).parameters

    for name in (
        "store",
        "sink",
        "describer",
        "queue",
        "location_sink",
        "photo_dir",
        "max_body_bytes",
        "host",
        "port",
        "env",
        "clock",
    ):
        assert name in parameters, f"{name} is not injectable"
    assert "request" not in parameters, "the bridge should not need a socket"


def test_the_request_handler_is_constructible_with_an_injected_bridge(tmp_path):
    sink = RecordingSink()
    describer = RecordingDescriber(image_description_seen)
    queue = ReplyQueue()
    b = make_bridge(tmp_path, sink=sink, describer=describer, queue=queue)

    handler_class = PhoneRequestHandler.for_bridge(b)

    assert issubclass(handler_class, PhoneRequestHandler)
    assert handler_class.bridge is b
    # The class attribute is what the adapter reads; no global or module state.
    assert "bridge" in vars(handler_class)


def test_the_handler_reads_the_bridge_from_the_server_when_not_bound(tmp_path):
    b = make_bridge(tmp_path)

    class FakeServer:
        pass

    handler = PhoneRequestHandler.__new__(PhoneRequestHandler)
    handler.server = FakeServer()
    handler.server.bridge = b
    PhoneRequestHandler.bridge = None

    assert handler.bridge_or_none() is b


def test_the_whole_request_path_runs_with_no_socket_at_all(tmp_path, monkeypatch):
    def bomb(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("handle() reached for a socket")

    monkeypatch.setattr(socket, "socket", bomb)
    monkeypatch.setattr(socket, "create_connection", bomb)

    sink = RecordingSink()
    locations: list[LocationRecord] = []
    b = make_bridge(
        tmp_path,
        sink=sink,
        describer=RecordingDescriber(image_description_seen),
        location_sink=locations.append,
    )

    assert b.handle("GET", "/health", headers=auth(b)).status == 200
    assert post_json(b, "/text", {"text": "hello"}).status == 202
    assert post_json(b, "/loc", {"latitude": 1.0, "longitude": 2.0}).status == 200
    assert b.handle("GET", "/reply", headers=auth(b)).status == 200
    assert b.handle(
        "POST", "/see", content_type="image/jpeg", body=FAKE_JPEG, headers=auth(b)
    ).status == 200

    assert len(sink.utterances) == 1
    assert len(locations) == 1


def test_the_package_re_exports_the_public_surface():
    import claire.phone as package

    assert package.PhoneBridge is bridge.PhoneBridge
    assert package.ReplyQueue is bridge.ReplyQueue
    assert package.DEFAULT_HOST == DEFAULT_HOST
    assert package.TOKEN_KEY_NAME == TOKEN_KEY_NAME
    assert ALLOWED_IMAGE_CONTENT_TYPES["image/jpeg"] == ".jpg"


# --------------------------------------------------------------------------- #
# The live server: the layer no other test in this file touches
# --------------------------------------------------------------------------- #
#
# Everything above drives ``PhoneBridge.handle()`` as an ordinary function call
# -- the module's own design, and a good one, but it means the socket-facing
# layer (``PhoneRequestHandler._serve``) had *no* coverage at all.  A real HTTP
# client sends no ``Content-Length`` with a bodyless ``GET``; ``_serve`` read the
# body unconditionally, so every ``GET /health`` and ``GET /reply`` over the wire
# was answered ``411 length_required`` before the route was even looked at -- and
# 96 in-process tests still passed.  These tests own that layer.

@contextlib.contextmanager
def live_bridge(bridge_obj):
    """A real HTTP server on an ephemeral loopback port, in a real thread."""
    server = bridge.create_server(bridge_obj, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get_over_socket(port: int, path: str, *, token=None):
    """A GET the way an ordinary client sends it: no Content-Length header."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer %s" % token
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_a_bodyless_get_health_is_answered_and_not_a_411(tmp_path):
    """GET /health with no Content-Length must reach authentication."""
    b = make_bridge(tmp_path)
    with live_bridge(b) as port:
        status, body = get_over_socket(port, "/health")

    assert status == 401, body
    assert json.loads(body)["error"]["code"] == "unauthorized"


def test_a_bodyless_get_health_with_the_token_answers_200(tmp_path):
    b = make_bridge(tmp_path)
    with live_bridge(b) as port:
        status, body = get_over_socket(port, "/health", token=b.token())

    assert status == 200, body
    payload = json.loads(body)
    assert payload["ok"] is True
    # The token is reported masked over the wire too, never raw.
    assert b.token() not in body.decode()


def test_a_bodyless_get_reply_drains_once_over_a_real_socket(tmp_path):
    b = make_bridge(tmp_path, queue=ReplyQueue(["first thing"]))
    with live_bridge(b) as port:
        status, body = get_over_socket(port, "/reply", token=b.token())
        second_status, second_body = get_over_socket(port, "/reply", token=b.token())

    assert status == 200, body
    assert json.loads(body)["count"] == 1
    assert second_status == 200
    assert json.loads(second_body)["items"] == []


def test_a_post_with_no_content_length_is_still_a_named_411(tmp_path):
    """The fix is deliberately narrow: an unframed body is still a 411."""
    b = make_bridge(tmp_path, sink=RecordingSink())
    request = (
        b"POST /text HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer " + b.token().encode() + b"\r\n"
        b"Connection: close\r\n\r\n"
    )
    with live_bridge(b) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            raw.sendall(request)
            raw.settimeout(5)
            chunks = []
            while True:
                chunk = raw.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)

    response = b"".join(chunks)
    head, _, body = response.partition(b"\r\n\r\n")
    assert b" 411 " in head.split(b"\r\n")[0], head
    assert json.loads(body)["error"]["code"] == "length_required"


# --------------------------------------------------------------------------- #
# A remedy is a promise that the command it names runs
# --------------------------------------------------------------------------- #

#: ``python -m module.name`` plus the trailing words, stopping at punctuation.
PYTHON_M_COMMAND = re.compile(r"python -m ([A-Za-z0-9_.]+)((?:\s+[A-Za-z0-9_.-]+)*)")


def test_every_remedy_names_a_python_m_command_that_really_parses(tmp_path):
    """Advice that says "run this" must name a command that runs.

    Three remedies told the owner to run ``python -m claire.cli keys ...`` on the
    laptop, but ``claire.cli`` accepts only ``--smoke-test`` and ``--version``:
    following the advice printed ``unrecognized arguments: keys``.  The command
    has to come from a module that implements it, and this test parses each one
    with the parser the named module actually exposes.
    """
    remedies = []

    # 1. No credentials were offered.
    b = make_bridge(tmp_path)
    with pytest.raises(AuthenticationError) as auth_error:
        b.authorize({})
    remedies.append(auth_error.value.remedy)

    # 2. The key store cannot be read at all (a directory where a file belongs).
    broken = tmp_path / "broken-store"
    broken.mkdir()
    unreadable = PhoneBridge(
        store=broken, env={}, host=DEFAULT_HOST, port=DEFAULT_PORT
    )
    with pytest.raises(bridge.TokenUnavailableError) as store_error:
        unreadable.token()
    remedies.append(store_error.value.remedy)

    # 3. The stored token is too weak to authenticate anyone.
    weak_store = KeyStore(tmp_path / "weak.json")
    weak_store.store(TOKEN_KEY_NAME, "tooshort")
    weak_bridge = PhoneBridge(
        store=weak_store, env={}, host=DEFAULT_HOST, port=DEFAULT_PORT
    )
    with pytest.raises(bridge.TokenUnavailableError) as weak_error:
        weak_bridge.token()
    remedies.append(weak_error.value.remedy)

    commands = [
        command for remedy in remedies for command in PYTHON_M_COMMAND.findall(remedy)
    ]
    # Every remedy above names at least one command; four in total.
    assert len(commands) >= 4, commands

    for module_name, tail in commands:
        # ``python -m pkg`` runs pkg/__main__.py, so a package's parser lives
        # there rather than in its __init__.
        module = importlib.import_module(module_name)
        parser_factory = getattr(module, "build_parser", None)
        if parser_factory is None and hasattr(module, "__path__"):
            module = importlib.import_module(module_name + ".__main__")
            parser_factory = getattr(module, "build_parser", None)
        assert parser_factory is not None, (
            "remedy names python -m %s, which exposes no build_parser: %r"
            % (module_name, commands)
        )
        # SystemExit here means the advice points at a command that does not exist.
        parser_factory().parse_args(tail.split())


# --------------------------------------------------------------------------- #
# /transcribe: audio in, the voice loop's own recogniser, text out
# --------------------------------------------------------------------------- #
#
# This is the one endpoint that is not a translation of something the phone can
# already do.  An always-listening client has *audio*, and the bridge used to
# accept only text -- so an app with no buttons had nothing to call.  The route
# reuses claire.listening.stt.create_stt_engine (asserted below by source
# inspection as well as by behaviour) rather than inventing a second
# transcription stack, and the tests here drive it through a real socket as well
# as through handle(), because in-process coverage is exactly how the 411 and
# HeaderMap bugs survived 96 passing tests.

class FakeRecogniser:
    """A stand-in for SttEngine: records what it was given, answers in words."""

    name = "fake_parakeet"

    def __init__(self, text: str = "what is the weather in lisbon") -> None:
        self.text = text
        self.calls = []

    def transcribe(self, samples, sample_rate):
        # The real contract is claire.listening.stt.SttResult; importing it here
        # keeps a 30-second NeMo import out of test collection.
        from claire.listening.stt import SttResult

        self.calls.append((samples, sample_rate))
        return SttResult(text=self.text, engine=self.name)


def wav_bytes(seconds: float = 0.5, rate: int = 16000, channels: int = 1,
              width: int = 2, amplitude: int = 8000) -> bytes:
    """A canonical RIFF/WAVE file, built the way the phone's ClaireWire builds it."""
    import io
    import math
    import wave

    frames = int(seconds * rate)
    payload = bytearray()
    for index in range(frames):
        value = int(amplitude * math.sin(2 * math.pi * 220.0 * index / rate))
        for _ in range(channels):
            if width == 1:
                payload.append((value >> 8) + 128)
            elif width == 2:
                payload += int(value).to_bytes(2, "little", signed=True)
            else:
                payload += (value << 16).to_bytes(4, "little", signed=True)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(bytes(payload))
    return buffer.getvalue()


def post_over_socket(port: int, path: str, body: bytes, content_type: str, *,
                     token=None, headers=None):
    """A POST the way an ordinary HTTP client sends it."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = "Bearer %s" % token
        if content_type is not None:
            sent["Content-Type"] = content_type
        conn.request("POST", path, body=body, headers=sent)
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()


def test_transcribe_round_trips_audio_over_a_real_socket(tmp_path):
    """The whole path: a socket, a WAV body, the recogniser, the sink."""
    recogniser = FakeRecogniser()
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink, transcriber=recogniser)

    with live_bridge(b) as port:
        status, payload = post_over_socket(
            port, "/transcribe", wav_bytes(seconds=0.5), "audio/wav", token=b.token()
        )

    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["text"] == "what is the weather in lisbon"
    assert payload["delivered"] is True and payload["accepted"] is True
    assert payload["engine"] == "fake_parakeet"
    assert payload["sample_rate"] == 16000
    assert abs(payload["seconds"] - 0.5) < 1e-6

    # The recogniser was handed real samples, not the bytes.
    assert len(recogniser.calls) == 1
    samples, rate = recogniser.calls[0]
    assert rate == 16000
    assert samples.shape == (8000,)
    assert abs(float(max(samples)) - 8000 / 32768.0) < 1e-3

    # ...and the words reached Claire's voice loop, tagged as a transcript.
    assert len(sink.utterances) == 1
    utterance = sink.utterances[0]
    assert utterance["kind"] == "transcript"
    assert utterance["text"] == "what is the weather in lisbon"
    assert utterance["source"] == "phone"
    assert utterance["engine"] == "fake_parakeet"


def test_transcribe_downmixes_stereo_to_mono(tmp_path):
    recogniser = FakeRecogniser()
    b = make_bridge(tmp_path, sink=RecordingSink(), transcriber=recogniser)

    response = b.handle(
        "POST",
        "/transcribe",
        content_type="audio/x-wav",
        body=wav_bytes(seconds=0.25, channels=2),
        headers=auth(b),
    )

    assert response.status == 200
    samples, rate = recogniser.calls[0]
    assert rate == 16000
    # 0.25 s at 16 kHz, downmixed from two channels to one.
    assert samples.shape == (4000,)


def test_transcribe_accepts_a_riff_body_declared_as_octet_stream(tmp_path):
    recogniser = FakeRecogniser()
    b = make_bridge(tmp_path, sink=RecordingSink(), transcriber=recogniser)

    response = b.handle(
        "POST",
        "/transcribe",
        content_type="application/octet-stream",
        body=wav_bytes(seconds=0.2),
        headers=auth(b),
    )

    assert response.status == 200
    assert response.payload["text"] == recogniser.text


def test_transcribe_refuses_an_mp3_with_a_named_415(tmp_path):
    b = make_bridge(tmp_path, sink=RecordingSink(), transcriber=FakeRecogniser())

    response = b.handle(
        "POST",
        "/transcribe",
        content_type="audio/mpeg",
        body=b"\xff\xfb\x90\x00" * 64,
        headers=auth(b),
    )

    assert response.status == 415
    assert error_code(response) == "unsupported_content_type"
    assert "audio/wav" in response.payload["error"]["remedy"]


def test_transcribe_refuses_a_truncated_wav_with_a_named_400(tmp_path):
    b = make_bridge(tmp_path, sink=RecordingSink(), transcriber=FakeRecogniser())
    broken = wav_bytes(seconds=0.5)[:40]

    response = b.handle(
        "POST", "/transcribe", content_type="audio/wav", body=broken, headers=auth(b)
    )

    assert response.status == 400
    assert error_code(response) == "invalid_audio"
    # The real reason, not a generic "bad request": this header promises a fmt
    # and a data chunk and stops before either.
    assert "WAV container could not be read" in response.payload["error"]["message"]


def test_transcribe_refuses_a_body_that_is_not_a_container_at_all(tmp_path):
    b = make_bridge(tmp_path, sink=RecordingSink(), transcriber=FakeRecogniser())

    response = b.handle(
        "POST",
        "/transcribe",
        content_type="audio/wav",
        body=b"this is a text file, honestly" * 8,
        headers=auth(b),
    )

    assert response.status == 400
    assert error_code(response) == "invalid_audio"
    assert "does not start with a RIFF/WAVE header" in response.payload["error"]["message"]


def test_transcribe_refuses_an_empty_body(tmp_path):
    b = make_bridge(tmp_path, sink=RecordingSink(), transcriber=FakeRecogniser())

    response = b.handle(
        "POST", "/transcribe", content_type="audio/wav", body=b"", headers=auth(b)
    )

    assert response.status == 400
    assert error_code(response) == "invalid_audio"


def test_transcribe_refuses_a_clip_too_short_to_recognise(tmp_path):
    recogniser = FakeRecogniser()
    b = make_bridge(tmp_path, sink=RecordingSink(), transcriber=recogniser)

    response = b.handle(
        "POST",
        "/transcribe",
        content_type="audio/wav",
        body=wav_bytes(seconds=0.01),
        headers=auth(b),
    )

    assert response.status == 400
    assert error_code(response) == "invalid_audio"
    assert "too short" in response.payload["error"]["message"]
    assert recogniser.calls == [], "a 10 ms clip was sent to a 0.6B model"


def test_transcribe_refuses_an_absurdly_long_clip(tmp_path):
    recogniser = FakeRecogniser()
    b = make_bridge(tmp_path, sink=RecordingSink(), transcriber=recogniser)

    response = b.handle(
        "POST",
        "/transcribe",
        content_type="audio/wav",
        body=wav_bytes(seconds=121.0),
        headers=auth(b),
    )

    assert response.status == 400
    assert "maximum for one utterance" in response.payload["error"]["message"]
    assert recogniser.calls == []


def test_transcribe_names_an_unavailable_recogniser_and_remembers_it(tmp_path):
    calls = []

    def factory():
        calls.append(1)
        raise RuntimeError("no model configured")

    b = make_bridge(
        tmp_path, sink=RecordingSink(), transcriber_factory=factory
    )

    first = b.handle(
        "POST", "/transcribe", content_type="audio/wav", body=wav_bytes(0.5),
        headers=auth(b),
    )
    second = b.handle(
        "POST", "/transcribe", content_type="audio/wav", body=wav_bytes(0.5),
        headers=auth(b),
    )

    assert first.status == 503
    assert error_code(first) == "transcription_unavailable"
    assert "no model configured" in first.payload["error"]["message"]
    assert second.status == 503
    assert len(calls) == 1, "the failed recogniser was rebuilt on every request"
    assert b.transcribe_status().startswith("failed:")


def test_transcribe_still_returns_the_words_when_no_sink_is_wired(tmp_path):
    """The transcript is the answer; delivery is reported, not pretended."""
    b = make_bridge(tmp_path, sink=None, transcriber=FakeRecogniser("hello Claire"))

    response = b.handle(
        "POST", "/transcribe", content_type="audio/wav", body=wav_bytes(0.5),
        headers=auth(b),
    )

    assert response.status == 200
    assert response.payload["text"] == "hello Claire"
    assert response.payload["accepted"] is False
    assert response.payload["delivered"] is False
    assert "no utterance sink" in response.payload["note"]


def test_transcribe_does_not_deliver_an_empty_transcript(tmp_path):
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink, transcriber=FakeRecogniser("   "))

    response = b.handle(
        "POST", "/transcribe", content_type="audio/wav", body=wav_bytes(0.5),
        headers=auth(b),
    )

    assert response.status == 200
    assert response.payload["text"] == ""
    assert response.payload["delivered"] is False
    assert "nothing intelligible" in response.payload["note"]
    assert sink.utterances == [], "silence was delivered to Claire as an utterance"


def test_transcribe_needs_a_token_and_touches_nothing_without_one(tmp_path):
    recogniser = FakeRecogniser()
    sink = RecordingSink()
    b = make_bridge(tmp_path, sink=sink, transcriber=recogniser)

    response = b.handle(
        "POST", "/transcribe", content_type="audio/wav", body=wav_bytes(0.5), headers={}
    )

    assert response.status == 401
    assert error_code(response) == "unauthorized"
    assert recogniser.calls == []
    assert sink.utterances == []


def test_transcribe_reports_a_recogniser_that_raises_as_a_named_502(tmp_path):
    class Exploding(FakeRecogniser):
        def transcribe(self, samples, sample_rate):
            raise RuntimeError("cuda melted")

    b = make_bridge(tmp_path, sink=RecordingSink(), transcriber=Exploding())

    response = b.handle(
        "POST", "/transcribe", content_type="audio/wav", body=wav_bytes(0.5),
        headers=auth(b),
    )

    assert response.status == 502
    assert error_code(response) == "transcription_failed"
    assert "cuda melted" in response.payload["error"]["message"]


def test_transcribe_uses_the_projects_own_stt_engine_and_not_a_second_stack(tmp_path):
    """A second transcription stack is the kind of duplication this forbids."""
    source = inspect.getsource(bridge.PhoneBridge._build_project_transcriber)

    assert "create_stt_engine" in source
    assert "listening.stt" in source
    # The bridge may not import a recogniser directly anywhere.
    module_source = inspect.getsource(bridge)
    assert "import whisper" not in module_source
    assert "parakeet" not in module_source.lower(), (
        "the bridge names a specific recogniser instead of asking "
        "create_stt_engine for the configured one"
    )


# --------------------------------------------------------------------------- #
# The endpoints the shipped 0.2 phone client calls
# --------------------------------------------------------------------------- #
#
# The APK in the owner's hand posts to /location and reads /where, and it
# authenticates with X-Claire-Token.  The bridge serves /loc and /reply with
# Authorization: Bearer.  Both sides are legitimate; the drift is not.  These
# tests pin the translation, over a real socket, so a working phone is not
# answered with a 404 or a 401 by a bridge that merely renamed things.

def test_location_is_loc_over_a_real_socket(tmp_path):
    seen = []
    b = make_bridge(tmp_path, sink=RecordingSink(), location_sink=seen.append)

    with live_bridge(b) as port:
        status, payload = post_over_socket(
            port,
            "/location",
            json.dumps({"latitude": 38.7223, "longitude": -9.1393,
                        "accuracy": 12.5}).encode("utf-8"),
            "application/json",
            token=b.token(),
        )

    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["recorded"] is True
    assert "38.7223" not in json.dumps(payload), "the fix was read back over the socket"
    assert seen and seen[0].latitude == pytest.approx(38.7223)


def test_where_drains_the_reply_queue_exactly_once_over_a_real_socket(tmp_path):
    queue = ReplyQueue()
    queue.put_text("the kettle is boiling")
    b = make_bridge(tmp_path, queue=queue)

    with live_bridge(b) as port:
        first_status, first_body = get_over_socket(port, "/where", token=b.token())
        second_status, second_body = get_over_socket(port, "/where", token=b.token())

    first = json.loads(first_body)
    second = json.loads(second_body)
    assert first_status == 200
    assert first["count"] == 1 and first["items"][0]["text"] == "the kettle is boiling"
    assert second_status == 200
    assert second["count"] == 0, "/where replayed a sentence the phone already showed"


def test_the_legacy_token_header_authenticates_over_a_real_socket(tmp_path):
    b = make_bridge(tmp_path, sink=RecordingSink())

    with live_bridge(b) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", "/health", headers={"X-Claire-Token": b.token()})
            response = conn.getresponse()
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    assert status == 200, payload
    assert payload["status"] == "ok"


def test_a_wrong_legacy_token_is_401_and_not_an_oracle(tmp_path):
    b = make_bridge(tmp_path)

    with live_bridge(b) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", "/health", headers={"X-Claire-Token": "wrong" * 8})
            response = conn.getresponse()
            wrong_status = response.status
            wrong_body = response.read()
        finally:
            conn.close()

    with live_bridge(b) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            missing_status = response.status
            missing_body = response.read()
        finally:
            conn.close()

    assert wrong_status == missing_status == 401
    assert wrong_body == missing_body, "the legacy header is an auth oracle"


def test_health_reports_the_transcribe_capability(tmp_path):
    configured = make_bridge(tmp_path, transcriber=FakeRecogniser())
    default = make_bridge(tmp_path / "other")

    response = configured.handle("GET", "/health", headers=auth(configured))

    assert response.status == 200
    assert response.payload["transcribe"] == "configured"
    assert default.transcribe_status() == "project_default"
    assert "/transcribe" in response.payload["endpoints"]
    assert response.payload["endpoints"]["/transcribe"] == ["POST"]


def test_the_phone_bridge_does_not_share_the_web_panels_port():
    """Two services on one port is a bind failure nobody can see coming."""
    from claire.panel import server as panel_server

    assert bridge.DEFAULT_PORT != panel_server.DEFAULT_PORT, (
        "the phone bridge and the web panel both default to port %d"
        % bridge.DEFAULT_PORT
    )
    # 8731 is held by `dsh web` on this machine and was hard-coded by the
    # shipped 0.2 phone client; it must not be the bridge's default either.
    assert bridge.DEFAULT_PORT != 8731


def test_the_transcribe_remedy_names_a_script_that_exists():
    """Advice that says "run this" must name a file that is there."""
    remedy = bridge.DEFAULT_TRANSCRIBE_REMEDY
    match = re.search(r"scripts/([A-Za-z0-9_./-]+\.py)", remedy)
    assert match, remedy
    script = Path(__file__).resolve().parent.parent / "scripts" / match.group(1)
    assert script.is_file(), "the remedy names a missing script: %s" % script


# --------------------------------------------------------------------------- #
# /say: the channel back to the phone, over a real socket
# --------------------------------------------------------------------------- #
#
# /say exists because the bridge normally runs as its own process, so the voice
# loop reaches the reply queue over HTTP like any other client.  These are
# exactly the tests that must not be in-process: a handler that never registered
# /say, or a socket layer that mishandles a JSON POST, would still pass a call to
# ``PhoneBridge.handle()`` and has to fail here.  Every case below drives the
# real ``ThreadingHTTPServer`` bound to an OS-chosen port on 127.0.0.1.

def post_raw_over_socket(port: int, path: str, body: bytes, content_type, *,
                         token=None):
    """A POST that returns the body as bytes, not parsed JSON.

    ``post_over_socket`` decodes for payload assertions; this one keeps the
    bytes so two refusals can be compared exactly, which is the whole point of
    the no-oracle check.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer %s" % token
        if content_type is not None:
            headers["Content-Type"] = content_type
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_say_queues_text_that_the_next_reply_drains_once_over_a_real_socket(
    tmp_path,
):
    b = make_bridge(tmp_path)

    with live_bridge(b) as port:
        status, payload = post_over_socket(
            port,
            "/say",
            b'{"text": "good morning"}',
            "application/json",
            token=b.token(),
        )
        first_status, first_body = get_over_socket(port, "/reply", token=b.token())
        second_status, second_body = get_over_socket(port, "/reply", token=b.token())

    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["queued"] == {"kind": "text", "text": "good morning"}
    assert payload["count"] == 1

    first = json.loads(first_body)
    assert first_status == 200, first
    assert first["count"] == 1
    assert first["items"] == [{"kind": "text", "text": "good morning"}]

    second = json.loads(second_body)
    assert second_status == 200, second
    assert second["count"] == 0, "/say's item was handed out more than once"


def test_say_queues_a_photo_command_with_its_question_over_a_real_socket(
    tmp_path,
):
    b = make_bridge(tmp_path)

    with live_bridge(b) as port:
        status, payload = post_over_socket(
            port,
            "/say",
            json.dumps({"command": "photo", "question": "what is this?"}).encode(
                "utf-8"
            ),
            "application/json",
            token=b.token(),
        )
        drain_status, drain_body = get_over_socket(port, "/reply", token=b.token())
        again_status, again_body = get_over_socket(port, "/reply", token=b.token())

    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["queued"]["kind"] == "command"
    assert payload["queued"]["command"] == "photo"
    assert payload["queued"]["question"] == "what is this?"
    assert payload["count"] == 1

    drained = json.loads(drain_body)
    assert drain_status == 200, drained
    assert drained["count"] == 1
    assert drained["items"][0]["kind"] == "command"
    assert drained["items"][0]["command"] == "photo"
    assert drained["items"][0]["question"] == "what is this?"
    assert again_status == 200
    assert json.loads(again_body)["count"] == 0


def test_say_queues_the_location_command_over_a_real_socket(tmp_path):
    b = make_bridge(tmp_path)

    with live_bridge(b) as port:
        status, payload = post_over_socket(
            port,
            "/say",
            b'{"command": "location"}',
            "application/json",
            token=b.token(),
        )
        drain_status, drain_body = get_over_socket(port, "/reply", token=b.token())

    assert status == 200, payload
    assert payload["queued"] == {"kind": "command", "command": "location"}
    assert payload["count"] == 1
    drained = json.loads(drain_body)
    assert drain_status == 200, drained
    assert drained["items"] == [{"kind": "command", "command": "location"}]


def test_say_explicit_command_kind_wins_and_keeps_the_text_over_a_real_socket(
    tmp_path,
):
    b = make_bridge(tmp_path)

    with live_bridge(b) as port:
        status, payload = post_over_socket(
            port,
            "/say",
            json.dumps(
                {"text": "look at this", "kind": "command", "command": "photo"}
            ).encode("utf-8"),
            "application/json",
            token=b.token(),
        )
        drain_status, drain_body = get_over_socket(port, "/reply", token=b.token())

    assert status == 200, payload
    assert payload["queued"]["kind"] == "command"
    assert payload["queued"]["command"] == "photo"
    assert payload["queued"]["text"] == "look at this"
    drained = json.loads(drain_body)
    assert drain_status == 200, drained
    assert drained["count"] == 1
    assert drained["items"][0]["kind"] == "command"
    assert drained["items"][0]["command"] == "photo"
    assert drained["items"][0]["text"] == "look at this"


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "video"},
        {"command": ""},
        {"kind": "command"},
        {"kind": "command", "command": "PHOTO"},
    ],
)
def test_say_refuses_an_unknown_or_missing_command_by_name_without_queueing(
    tmp_path, payload
):
    b = make_bridge(tmp_path)

    with live_bridge(b) as port:
        status, answer = post_over_socket(
            port,
            "/say",
            json.dumps(payload).encode("utf-8"),
            "application/json",
            token=b.token(),
        )
        empty_status, empty_body = get_over_socket(port, "/reply", token=b.token())

    assert status == 400, answer
    assert answer["ok"] is False
    assert answer["error"]["code"]
    # The body names both commands, so a caller never has to read the source.
    shown = json.dumps(answer)
    assert "photo" in shown
    assert "location" in shown
    assert empty_status == 200
    assert json.loads(empty_body)["count"] == 0, "a refused /say queued something"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"id": 7},
        {"kind": "text"},
        {"text": ""},
        {"text": "   "},
    ],
)
def test_say_refuses_a_body_with_nothing_to_queue(tmp_path, payload):
    b = make_bridge(tmp_path)

    with live_bridge(b) as port:
        status, answer = post_over_socket(
            port,
            "/say",
            json.dumps(payload).encode("utf-8"),
            "application/json",
            token=b.token(),
        )
        empty_status, empty_body = get_over_socket(port, "/reply", token=b.token())

    assert status == 400, answer
    assert answer["ok"] is False
    assert answer["error"]["code"]
    assert empty_status == 200
    assert json.loads(empty_body)["count"] == 0


def test_say_without_a_token_is_the_same_401_as_health_over_a_real_socket(
    tmp_path,
):
    b = make_bridge(tmp_path)
    body = b'{"text": "good morning"}'

    with live_bridge(b) as port:
        missing_status, missing_body = post_raw_over_socket(
            port, "/say", body, "application/json"
        )
        wrong_status, wrong_body = post_raw_over_socket(
            port, "/say", body, "application/json", token="wrong" * 8
        )
        health_status, health_body = get_over_socket(
            port, "/health", token="wrong" * 8
        )
        reply_status, reply_body = get_over_socket(port, "/reply", token=b.token())

    assert missing_status == 401, missing_body
    assert wrong_status == 401, wrong_body
    assert health_status == 401, health_body
    assert wrong_body == health_body, "the 401 is an oracle: /say differs from /health"
    assert missing_body == wrong_body
    assert b.token().encode() not in wrong_body
    assert reply_status == 200
    assert json.loads(reply_body)["count"] == 0, "a refused /say reached the queue"


def test_the_canonical_and_legacy_routes_still_behave_over_a_real_socket(tmp_path):
    queue = ReplyQueue()
    queue.put_text("the kettle is boiling")
    seen = []
    b = make_bridge(tmp_path, queue=queue, location_sink=seen.append)

    with live_bridge(b) as port:
        health_status, health_body = get_over_socket(port, "/health", token=b.token())
        reply_status, reply_body = get_over_socket(port, "/reply", token=b.token())
        where_status, where_body = get_over_socket(port, "/where", token=b.token())
        location_status, location_payload = post_over_socket(
            port,
            "/location",
            json.dumps(
                {"latitude": 38.7223, "longitude": -9.1393, "accuracy": 12.5}
            ).encode("utf-8"),
            "application/json",
            token=b.token(),
        )

    health = json.loads(health_body)
    assert health_status == 200, health
    assert health["status"] == "ok"
    assert health["endpoints"]["/say"] == ["POST"]

    reply = json.loads(reply_body)
    assert reply_status == 200, reply
    assert reply["count"] == 1
    assert reply["items"][0]["text"] == "the kettle is boiling"

    where = json.loads(where_body)
    assert where_status == 200, where
    assert where["count"] == 0, "/where replayed a sentence the phone already showed"

    assert location_status == 200, location_payload
    assert location_payload["recorded"] is True
    assert seen and seen[0].latitude == pytest.approx(38.7223)


@pytest.mark.parametrize(
    "content_type, body, expected",
    [
        ("text/plain", b'{"text": "hi"}', 415),
        (None, b'{"text": "hi"}', 415),
        ("application/json", b"not json at all", 400),
        ("application/json", b"", 400),
        ("application/json", b"[1, 2, 3]", 400),
    ],
)
def test_say_reuses_the_text_routes_body_handling_exactly(
    tmp_path, content_type, body, expected
):
    b = make_bridge(tmp_path)

    with live_bridge(b) as port:
        say_status, say_body = post_raw_over_socket(
            port, "/say", body, content_type, token=b.token()
        )
        text_status, text_body = post_raw_over_socket(
            port, "/text", body, content_type, token=b.token()
        )

    assert say_status == text_status == expected, (say_body, text_body)
    assert json.loads(say_body)["error"]["code"] == json.loads(text_body)["error"][
        "code"
    ], "the same refusal is reported two different ways"


def test_say_refuses_an_oversize_body_exactly_like_text(tmp_path):
    b = make_bridge(tmp_path, max_body_bytes=32)
    body = b'{"text": "' + b"x" * 64 + b'"}'

    with live_bridge(b) as port:
        say_status, say_body = post_raw_over_socket(
            port, "/say", body, "application/json", token=b.token()
        )
        text_status, text_body = post_raw_over_socket(
            port, "/text", body, "application/json", token=b.token()
        )

    assert say_status == text_status == 413, (say_body, text_body)
    assert json.loads(say_body)["error"]["code"] == json.loads(text_body)["error"][
        "code"
    ]


def test_say_with_an_unwired_queue_is_a_named_503(tmp_path):
    b = make_bridge(tmp_path)
    b._queue = None  # the construction the guard exists for

    response = post_json(b, "/say", {"text": "good morning"})

    assert response.status == 503
    assert error_code(response) == "queue_unconfigured"


def test_say_is_not_an_alias_and_the_alias_table_is_unchanged():
    assert "/say" not in bridge.ROUTE_ALIASES
    assert bridge.ENDPOINTS["/say"] == ("POST",)
    assert bridge.ROUTE_ALIASES["/location"] == "/loc"
    assert bridge.ROUTE_ALIASES["/where"] == "/reply"


def test_a_say_post_with_no_content_length_is_still_a_named_411(tmp_path):
    """The unframed-body rule is shared with /text, not re-implemented."""
    b = make_bridge(tmp_path)
    request = (
        b"POST /say HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer " + b.token().encode() + b"\r\n"
        b"Connection: close\r\n\r\n"
    )
    with live_bridge(b) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            raw.sendall(request)
            raw.settimeout(5)
            chunks = []
            while True:
                chunk = raw.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)

    response = b"".join(chunks)
    head, _, body = response.partition(b"\r\n\r\n")
    assert b" 411 " in head.split(b"\r\n")[0], head
    assert json.loads(body)["error"]["code"] == "length_required"
