"""Tests for :mod:`claire.output.fish_client` (decision D6).

Everything here is offline: the requests session is faked, so no test can reach
Fish Audio. The point of the suite is the failure policy, because that is where
TTS goes quietly wrong:

* a dead key must rotate the key and never the voice;
* a bad request must not burn the rest of the pool;
* a 200 carrying JSON is an error body, not audio;
* a stream with zero bytes is a failure, not an empty success;
* the first-chunk budget must fire long before the total budget;
* no key value may appear in any raised exception.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from claire.output import fish_client
from claire.output.fish_client import (
    FishApiError,
    FishClient,
    FishConfigError,
    FishEmptyStreamError,
    FishError,
    FishErrorBodyError,
    FishFirstChunkTimeoutError,
    FishKeyFileEmptyError,
    FishKeyFileMissingError,
    FishKeyFilePermissionsError,
    FishKeyPool,
    FishKeyPoolExhaustedError,
    FishRequestRejectedError,
    FishTotalTimeoutError,
    redact,
)

KEY_ONE = "sk-fish-AAAA1111BBBB2222"
KEY_TWO = "sk-fish-CCCC3333DDDD4444"
KEY_THREE = "sk-fish-EEEE5555FFFF6666"
ALL_KEYS = (KEY_ONE, KEY_TWO, KEY_THREE)

VOICE = "voice-cloned-9f3c"
MODEL = "s1"


# --------------------------------------------------------------------------
# Fakes: a requests session with no network behind it
# --------------------------------------------------------------------------


class FakeResponse:
    """The subset of ``requests.Response`` the client uses."""

    def __init__(
        self,
        status_code: int = 200,
        chunks=(),
        delays=None,
        error_body=None,
        post_delay: float = 0.0,
    ) -> None:
        self.status_code = status_code
        self._chunks = tuple(chunks)
        self._delays = list(delays) if delays is not None else []
        self.post_delay = post_delay
        self.closed = False
        self.iter_calls = 0
        self._error_body = error_body
        if error_body is not None:
            self.text = json.dumps(error_body)
        else:
            self.text = ""

    def iter_content(self, chunk_size=None):
        self.iter_calls += 1
        for index, chunk in enumerate(self._chunks):
            delay = self._delays[index] if index < len(self._delays) else 0.0
            if delay:
                time.sleep(delay)
            yield chunk

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Records every POST and hands back queued responses in order."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, stream=False, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers or {}),
                "json": json,
                "stream": stream,
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError(
                f"unexpected extra POST to {url!r}: the client made "
                f"{len(self.calls)} call(s) but only {len(self.calls) - 1} response(s) were queued"
            )
        response = self._responses.pop(0)
        if response.post_delay:
            time.sleep(response.post_delay)
        return response


def write_key_file(tmp_path, keys=ALL_KEYS, name="fish_api_key", mode=0o600):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(keys) + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def make_client(tmp_path, responses, keys=ALL_KEYS, **kwargs):
    pool = FishKeyPool(write_key_file(tmp_path, keys))
    session = FakeSession(responses)
    return FishClient(pool, session=session, **kwargs), session


def drain(client, **kwargs):
    return list(client.synthesise("hello there", VOICE, **kwargs))


# --------------------------------------------------------------------------
# Success path
# --------------------------------------------------------------------------


def test_success_returns_audio_chunks(tmp_path):
    client, session = make_client(
        tmp_path, [FakeResponse(200, chunks=(b"RIFF", b"\x00\x01", b"data"))]
    )
    chunks = drain(client)
    assert chunks == [b"RIFF", b"\x00\x01", b"data"]
    assert b"".join(chunks) == b"RIFF\x00\x01data"
    assert len(session.calls) == 1


def test_success_sends_text_voice_and_latency_in_the_body(tmp_path):
    client, session = make_client(tmp_path, [FakeResponse(200, chunks=(b"RIFF",))])
    drain(client, latency="normal", model="s2-pro")
    call = session.calls[0]
    assert call["url"] == fish_client.ENDPOINT
    assert call["stream"] is True
    assert call["headers"]["model"] == "s2-pro"
    assert call["headers"]["Authorization"] == f"Bearer {KEY_ONE}"
    assert call["json"] == {
        "text": "hello there",
        fish_client.VOICE_FIELD: VOICE,
        "latency": "normal",
    }
    assert call["json"][fish_client.VOICE_FIELD] == VOICE


def test_client_builds_a_real_requests_session_by_default(monkeypatch, tmp_path):
    """The default path is exercised: no session is injected."""
    fake = FakeSession([FakeResponse(200, chunks=(b"RIFF",))])
    created = []

    def factory():
        created.append(fake)
        return fake

    monkeypatch.setattr(fish_client.requests, "Session", factory)
    client = FishClient(FishKeyPool(write_key_file(tmp_path)))
    assert b"".join(drain(client)) == b"RIFF"
    assert created == [fake]


# --------------------------------------------------------------------------
# Key failover
# --------------------------------------------------------------------------


def test_401_then_200_rotates_key_and_keeps_the_same_voice_id(tmp_path):
    client, session = make_client(
        tmp_path,
        [
            FakeResponse(401, error_body={"message": "invalid api key"}),
            FakeResponse(200, chunks=(b"RIFF", b"audio")),
        ],
    )
    chunks = drain(client, first_chunk_timeout=2.0)

    assert chunks == [b"RIFF", b"audio"]
    assert len(session.calls) == 2
    # The key rotated...
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {KEY_ONE}"
    assert session.calls[1]["headers"]["Authorization"] == f"Bearer {KEY_TWO}"
    # ...and nothing else did. The voice id is identical in both attempts.
    assert session.calls[0]["json"][fish_client.VOICE_FIELD] == VOICE
    assert session.calls[1]["json"][fish_client.VOICE_FIELD] == VOICE
    assert session.calls[0]["json"]["text"] == session.calls[1]["json"]["text"]


def test_first_chunk_clock_restarts_for_the_next_key_after_a_failover(tmp_path):
    """A slow dead key must not eat the replacement key's first-byte budget.

    The 401 costs 0.15s; the good key then takes 0.15s to produce audio. With a
    single global 0.2s first-byte clock the second attempt would have had only
    0.05s left and failed, so this pins the per-attempt clock.
    """
    client, session = make_client(
        tmp_path,
        [
            FakeResponse(401, post_delay=0.15),
            FakeResponse(200, chunks=(b"RIFF",), delays=[0.15]),
        ],
    )
    assert drain(client, first_chunk_timeout=0.2, total_timeout=5.0) == [b"RIFF"]
    assert len(session.calls) == 2
    assert session.calls[1]["headers"]["Authorization"] == f"Bearer {KEY_TWO}"
    assert session.calls[1]["json"][fish_client.VOICE_FIELD] == VOICE


def test_every_dead_key_status_rotates(tmp_path):
    for status in sorted(fish_client.DEAD_KEY_STATUSES):
        client, session = make_client(
            tmp_path / f"dead-{status}",
            [FakeResponse(status), FakeResponse(200, chunks=(b"RIFF",))],
        )
        assert drain(client) == [b"RIFF"]
        assert len(session.calls) == 2, f"HTTP {status} should have rotated the key"
        assert session.calls[1]["json"][fish_client.VOICE_FIELD] == VOICE


def test_all_keys_dead_raises_pool_exhausted_naming_the_attempt_count(tmp_path):
    client, session = make_client(
        tmp_path, [FakeResponse(401), FakeResponse(403), FakeResponse(429)]
    )
    with pytest.raises(FishKeyPoolExhaustedError) as excinfo:
        drain(client)
    assert len(session.calls) == len(ALL_KEYS)
    message = str(excinfo.value)
    assert str(len(ALL_KEYS)) in message
    for key in ALL_KEYS:
        assert key not in message


def test_400_raises_immediately_without_a_second_key(tmp_path):
    """A bad request is not a bad key: no other key may be tried."""
    client, session = make_client(
        tmp_path,
        [FakeResponse(400, error_body={"message": "reference_id not found"})],
    )
    with pytest.raises(FishRequestRejectedError) as excinfo:
        drain(client)
    assert len(session.calls) == 1, "a 400 must not burn another key"
    assert "400" in str(excinfo.value)
    assert "reference_id not found" in str(excinfo.value)
    for key in ALL_KEYS:
        assert key not in str(excinfo.value)


@pytest.mark.parametrize("status", sorted(fish_client.BAD_REQUEST_STATUSES))
def test_bad_request_statuses_never_rotate(status, tmp_path):
    client, session = make_client(tmp_path, [FakeResponse(status)])
    with pytest.raises(FishRequestRejectedError):
        drain(client)
    assert len(session.calls) == 1


def test_transient_500_retries_once_on_the_same_key_then_raises(tmp_path):
    client, session = make_client(tmp_path, [FakeResponse(500), FakeResponse(500)])
    with pytest.raises(FishApiError) as excinfo:
        drain(client)
    assert "transient" in str(excinfo.value)
    assert [c["headers"]["Authorization"] for c in session.calls] == [
        f"Bearer {KEY_ONE}",
        f"Bearer {KEY_ONE}",
    ], "a transient failure retries the SAME key once, it does not rotate"


def test_transient_500_then_200_succeeds_on_the_same_key(tmp_path):
    client, session = make_client(
        tmp_path, [FakeResponse(500), FakeResponse(200, chunks=(b"RIFF",))]
    )
    assert drain(client) == [b"RIFF"]
    assert [c["headers"]["Authorization"] for c in session.calls] == [
        f"Bearer {KEY_ONE}",
        f"Bearer {KEY_ONE}",
    ]


# --------------------------------------------------------------------------
# Body and stream failures
# --------------------------------------------------------------------------


def test_json_body_with_status_200_fails(tmp_path):
    """HTTP 200 is not evidence of audio; the first byte is."""
    body = {"message": "Invalid reference audio", "status": 400}
    client, session = make_client(
        tmp_path,
        [
            FakeResponse(200, chunks=(json.dumps(body).encode(),)),
            FakeResponse(200, chunks=(json.dumps(body).encode(),)),
        ],
    )
    with pytest.raises(FishErrorBodyError) as excinfo:
        drain(client)
    message = str(excinfo.value)
    assert "not audio" in message
    assert "Invalid reference audio" in message
    assert len(session.calls) == 2


def test_json_body_after_leading_whitespace_also_fails(tmp_path):
    client, session = make_client(
        tmp_path,
        [
            FakeResponse(200, chunks=(b"\n  ", b'{"message": "nope"}')),
            FakeResponse(200, chunks=(b"\n  ", b'{"message": "nope"}')),
        ],
    )
    with pytest.raises(FishErrorBodyError):
        drain(client)


def test_zero_byte_stream_is_a_failure_not_an_empty_success(tmp_path):
    client, session = make_client(tmp_path, [FakeResponse(200, chunks=()), FakeResponse(200, chunks=())])
    with pytest.raises(FishEmptyStreamError) as excinfo:
        drain(client)
    message = str(excinfo.value)
    assert "zero bytes" in message
    assert "not an empty success" in message
    assert len(session.calls) == 2


def test_whitespace_only_stream_is_a_failure(tmp_path):
    client, session = make_client(
        tmp_path, [FakeResponse(200, chunks=(b"\n", b"  ")), FakeResponse(200, chunks=(b"\n",))]
    )
    with pytest.raises(FishEmptyStreamError):
        drain(client)


def test_stream_error_after_some_audio_does_not_replay_the_audio(tmp_path):
    class ExplodingResponse(FakeResponse):
        def iter_content(self, chunk_size=None):
            self.iter_calls += 1
            yield b"RIFF"
            raise ConnectionError("socket died mid-stream")

    response = ExplodingResponse(200)
    client, session = make_client(tmp_path, [response, FakeResponse(200, chunks=(b"RIFF",))])
    collected = []
    with pytest.raises(FishError):
        for chunk in client.synthesise("hello there", VOICE):
            collected.append(chunk)
    assert collected == [b"RIFF"], "audio already yielded must not be replayed on a retry"
    assert len(session.calls) == 1


# --------------------------------------------------------------------------
# Timeouts
# --------------------------------------------------------------------------


def test_first_chunk_timeout_fires_well_before_the_total_budget(tmp_path):
    # One chunk, but it will not appear for 0.4s.
    response = FakeResponse(200, chunks=(b"RIFF",), delays=[0.4])
    client, session = make_client(tmp_path, [response, FakeResponse(200, chunks=(b"RIFF",), delays=[0.4])])
    started = time.monotonic()
    with pytest.raises(FishFirstChunkTimeoutError) as excinfo:
        drain(client, first_chunk_timeout=0.05, total_timeout=30.0)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"first-chunk timeout took {elapsed:.2f}s despite a 0.05s budget"
    assert "No first audio byte" in str(excinfo.value)
    assert "0.05" in str(excinfo.value)
    # The transient retry rule applies: the same key was tried twice.
    assert len(session.calls) == 2


def test_total_timeout_fires_after_the_first_byte(tmp_path):
    response = FakeResponse(200, chunks=(b"RIFF", b"more"), delays=[0.02, 0.6])
    client, session = make_client(tmp_path, [response])
    started = time.monotonic()
    with pytest.raises(FishTotalTimeoutError) as excinfo:
        drain(client, first_chunk_timeout=0.1, total_timeout=0.25)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"total timeout took {elapsed:.2f}s despite a 0.25s budget"
    assert "total budget" in str(excinfo.value)
    assert len(session.calls) == 1, "no retry once audio has already been yielded"


def test_socket_read_timeout_is_bounded_by_the_first_chunk_budget(tmp_path):
    client, session = make_client(
        tmp_path, [FakeResponse(200, chunks=(b"RIFF",))], connect_timeout=1.0
    )
    drain(client, first_chunk_timeout=8.0, total_timeout=60.0)
    connect, read = session.calls[0]["timeout"]
    assert read == pytest.approx(8.0)
    assert connect == pytest.approx(1.0)
    assert connect < read


def test_bad_timeout_arguments_are_rejected_before_any_request(tmp_path):
    client, session = make_client(tmp_path, [])
    with pytest.raises(FishConfigError):
        client.synthesise("hi", VOICE, first_chunk_timeout=0)
    with pytest.raises(FishConfigError):
        client.synthesise("hi", VOICE, total_timeout=-1)
    with pytest.raises(FishConfigError):
        client.synthesise("hi", VOICE, first_chunk_timeout=10.0, total_timeout=5.0)
    assert session.calls == []


def test_empty_text_is_refused_without_burning_a_key(tmp_path):
    client, session = make_client(tmp_path, [])
    with pytest.raises(FishRequestRejectedError):
        client.synthesise("   ", VOICE)
    assert session.calls == []


# --------------------------------------------------------------------------
# Key pool: file handling and redaction
# --------------------------------------------------------------------------


def test_missing_key_file_raises_naming_the_exact_path(tmp_path):
    missing = tmp_path / "does" / "not" / "exist.keys"
    with pytest.raises(FishKeyFileMissingError) as excinfo:
        FishKeyPool(missing)
    assert str(missing) in str(excinfo.value)
    assert excinfo.value.path == str(missing)


def test_key_file_with_no_keys_raises_naming_the_path(tmp_path):
    path = tmp_path / "empty.keys"
    path.write_text("# only a comment\n\n   \n", encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(FishKeyFileEmptyError) as excinfo:
        FishKeyPool(path)
    assert str(path) in str(excinfo.value)


def test_key_file_skips_blanks_and_comments(tmp_path):
    path = tmp_path / "keys"
    path.write_text(
        f"# primary\n{KEY_ONE}\n\n   \n# backup\n{KEY_TWO}\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    pool = FishKeyPool(path)
    assert len(pool) == 2
    assert [pool.next_key() for _ in range(4)] == [KEY_ONE, KEY_TWO, KEY_ONE, KEY_TWO]


def test_group_readable_key_file_is_refused_unless_explicitly_allowed(tmp_path):
    path = write_key_file(tmp_path, mode=0o644)
    with pytest.raises(FishKeyFilePermissionsError) as excinfo:
        FishKeyPool(path)
    assert str(path) in str(excinfo.value)
    assert "0600" in str(excinfo.value)
    assert len(FishKeyPool(path, require_private_mode=False)) == len(ALL_KEYS)


def test_pool_repr_and_str_never_contain_a_key(tmp_path):
    client, session = make_client(tmp_path, [FakeResponse(200, chunks=(b"RIFF",))])
    pool = client.key_pool
    for text in (repr(pool), str(pool), f"{pool}", pool.__repr__()):
        for key in ALL_KEYS:
            assert key not in text
    assert "keys=3" in repr(pool)


def test_redact_keeps_only_last_four_and_a_fingerprint():
    tag = redact(KEY_ONE)
    assert KEY_ONE not in tag
    assert tag.split(":")[0].endswith(KEY_ONE[-4:])
    assert fish_client.fingerprint(KEY_ONE) in tag
    # A short secret is not mostly revealed: no tail is shown at all.
    short = redact("abc")
    assert short.startswith("<redacted:")
    assert not short.endswith("abc")
    assert "abc" not in short.replace(fish_client.fingerprint("abc"), "")


def test_pool_redact_text_scrubs_keys_from_vendor_text(tmp_path):
    pool = FishKeyPool(write_key_file(tmp_path))
    dirty = f"upstream said: Bearer {KEY_TWO} is invalid"
    cleaned = pool.redact_text(dirty)
    assert KEY_TWO not in cleaned
    assert "invalid" in cleaned


# --------------------------------------------------------------------------
# No key value may ever appear in an exception
# --------------------------------------------------------------------------


def _error_scenarios(tmp_path):
    """(label, responses) for every failure this module can raise."""
    return [
        ("missing-file", None),
        ("dead-key", [FakeResponse(401), FakeResponse(401), FakeResponse(401)]),
        ("all-dead", [FakeResponse(401), FakeResponse(403), FakeResponse(429)]),
        (
            "bad-request",
            [FakeResponse(422, error_body={"message": f"bad voice; key {KEY_ONE} rejected"})],
        ),
        ("transient", [FakeResponse(500), FakeResponse(500)]),
        (
            "json-200",
            [
                FakeResponse(200, chunks=(b'{"message": "echo ' + KEY_TWO.encode() + b'"}',)),
                FakeResponse(200, chunks=(b'{"message": "echo ' + KEY_TWO.encode() + b'"}',)),
            ],
        ),
        ("empty-stream", [FakeResponse(200, chunks=()), FakeResponse(200, chunks=())]),
        (
            "first-chunk-timeout",
            [
                FakeResponse(200, chunks=(b"RIFF",), delays=[0.3]),
                FakeResponse(200, chunks=(b"RIFF",), delays=[0.3]),
            ],
        ),
    ]


def test_no_key_value_ever_appears_in_a_raised_exception(tmp_path):
    errors = []
    for label, responses in _error_scenarios(tmp_path):
        if responses is None:
            try:
                FishKeyPool(tmp_path / f"absent-{label}.keys")
            except FishError as exc:  # pragma: no cover - must raise
                errors.append((label, exc))
            continue
        client, _ = make_client(tmp_path / label, responses)
        try:
            drain(client, first_chunk_timeout=0.05, total_timeout=0.2)
        except FishError as exc:
            errors.append((label, exc))

    assert len(errors) == len(_error_scenarios(tmp_path))
    for label, exc in errors:
        rendered = f"{exc} {exc!r} {type(exc).__name__} {exc.args!r}"
        for key in ALL_KEYS:
            assert key not in rendered, f"key leaked in the {label} failure: {rendered}"
            assert key[:12] not in rendered, f"key prefix leaked in the {label} failure"
        assert rendered.strip(), f"{label} produced an empty error message"


def test_key_pool_never_surfaces_the_value_in_a_traceback_string(tmp_path):
    """repr() of the pool is what a traceback prints for a frame's locals."""
    pool = FishKeyPool(write_key_file(tmp_path, keys=(KEY_ONE,)))
    try:
        client = FishClient(pool, session=FakeSession([FakeResponse(401)]))
        drain(client)
    except FishKeyPoolExhaustedError as exc:
        assert KEY_ONE not in f"{exc!r}"
        assert KEY_ONE not in "".join(str(a) for a in exc.args)
    else:  # pragma: no cover - the pool has one key and it is dead
        raise AssertionError("expected the single dead key to exhaust the pool")
