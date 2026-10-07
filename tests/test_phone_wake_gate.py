"""The phone must answer only what was addressed to it, and never drop the owner.

Two defects live in this file, and both were observed on the real device before
they were fixed:

* **The feedback loop.** The phone's microphone is always open, so
  ``POST /transcribe`` carries the whole room -- including Claire's own voice
  coming out of the phone's speaker.  Every transcript went straight to the reply
  engine, so she heard herself, answered, heard the answer and answered again.
  These tests pin the gate that stops it: a wake word for ambient audio, and an
  echo window while she is speaking.
* **The dropped words.** ``submit_text_query`` rejected a second submission while
  one was running, and the phone turned that into "send that again in a moment" --
  which throws away what the owner said.  These tests pin the queue.

The HTTP half is driven through a **real socket** on purpose: this codebase has
twice shipped a bug that in-process tests could not see, so the bridge's own
handler is exercised over a real ``ThreadingHTTPServer``.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import math
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from claire import daemon
from claire.phone import bridge
from claire.phone.bridge import PhoneBridge
from claire.platform.keys import KeyStore

pytestmark = pytest.mark.unit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

class FakeRecogniser:
    """A stand-in for the loop's recogniser that answers in whatever text is set."""

    name = "fake_parakeet"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, samples, sample_rate):
        from claire.listening.stt import SttResult

        self.calls += 1
        return SttResult(text=self.text, engine=self.name)


class FakeTts:
    """The smallest thing ``is_speaking()`` is asked of."""

    def __init__(self, speaking: bool) -> None:
        self.speaking = speaking

    def is_speaking(self) -> bool:
        return self.speaking


def wav_bytes(seconds: float = 0.25, rate: int = 16000) -> bytes:
    """A canonical RIFF/WAVE file, the shape the phone's ClaireWire builds."""
    frames = int(seconds * rate)
    payload = bytearray()
    for index in range(frames):
        value = int(8000 * math.sin(2 * math.pi * 220.0 * index / rate))
        payload += int(value).to_bytes(2, "little", signed=True)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(payload))
    return buffer.getvalue()


def make_bridge(tmp_path: Path, *, sink, transcriber) -> PhoneBridge:
    return PhoneBridge(
        store=KeyStore(tmp_path / "keys.json"),
        sink=sink,
        transcriber=transcriber,
        photo_dir=tmp_path / "photos",
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        env={},
        clock=lambda: 1_700_000_000.0,
    )


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


def post_audio(port: int, body: bytes, token: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "POST",
            "/transcribe",
            body=body,
            headers={
                "Content-Type": "audio/wav",
                "Content-Length": str(len(body)),
                "Authorization": f"Bearer {token}",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload
    finally:
        conn.close()


def post_text(port: int, text: str, token: str):
    body = json.dumps({"text": text}).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "POST",
            "/text",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Authorization": f"Bearer {token}",
            },
        )
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()


@pytest.fixture()
def phone(tmp_path, monkeypatch):
    """The daemon's phone path, wired to a real bridge, with the engine stubbed.

    ``_phone_answer`` is replaced with a recorder so these tests can assert what
    the gate decided and in what order, without a dialogue memory, a database or
    a model.  The HTTP layer is real; only the reply engine is a stand-in, and it
    is a stand-in for exactly the thing that is not under test.
    """
    answered: list = []
    monkeypatch.setattr(daemon, "_phone_answer", answered.append, raising=False)
    monkeypatch.setattr(daemon, "_phone_bridge", None, raising=False)
    monkeypatch.setattr(
        daemon,
        "_global_cfg",
        SimpleNamespace(
            wake_word="claire",
            wake_aliases=["clair", "clare", "klair"],
            wake_fuzzy_ratio=0.82,
        ),
        raising=False,
    )
    monkeypatch.setattr(daemon, "_global_tts_engine", FakeTts(False), raising=False)
    with daemon._phone_lock:
        daemon._PHONE_AMBIENT.clear()
        daemon._phone_queue.clear()
        daemon._phone_was_speaking = False
        daemon._phone_speech_ended_at = 0.0
        daemon._phone_dispatching = False
    return SimpleNamespace(answered=answered, monkeypatch=monkeypatch)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------- #
# the decision, at every corner
# --------------------------------------------------------------------------- #

def test_ambient_speech_without_the_wake_word_is_not_answered():
    verdict = daemon.phone_utterance_decision(
        "so anyway the kettle is still broken",
        kind="transcript",
        speaking=False,
        seconds_since_speech=None,
        wake_word="claire",
        aliases=["clair", "clare"],
        fuzzy_ratio=0.82,
    )
    assert verdict == "ambient"


def test_speech_that_names_her_is_answered():
    verdict = daemon.phone_utterance_decision(
        "claire what is the weather",
        kind="transcript",
        speaking=False,
        seconds_since_speech=None,
        wake_word="claire",
        aliases=["clair"],
        fuzzy_ratio=0.82,
    )
    assert verdict == "answer"


def test_a_known_mishearing_of_her_name_still_wakes_her():
    verdict = daemon.phone_utterance_decision(
        "clair what is the weather",
        kind="transcript",
        speaking=False,
        seconds_since_speech=None,
        wake_word="claire",
        aliases=["clair"],
        fuzzy_ratio=0.82,
    )
    assert verdict == "answer"


def test_a_fuzzy_mishearing_of_the_name_wakes_her():
    # "klair" is not in the alias list here; it is the fuzzy matcher's job, and
    # it is the loop's own matcher rather than a second one written for the phone.
    verdict = daemon.phone_utterance_decision(
        "klair tell me a joke",
        kind="transcript",
        speaking=False,
        seconds_since_speech=None,
        wake_word="claire",
        aliases=[],
        fuzzy_ratio=0.82,
    )
    assert verdict == "answer"


def test_ordinary_english_that_merely_sounds_like_her_name_is_not_answered():
    # The regression the wake-detection module's header documents: loosening the
    # fuzzy ratio woke her on "I do not care".
    verdict = daemon.phone_utterance_decision(
        "i do not care about that at all",
        kind="transcript",
        speaking=False,
        seconds_since_speech=None,
        wake_word="claire",
        aliases=[],
        fuzzy_ratio=0.82,
    )
    assert verdict == "ambient"


def test_a_transcript_that_arrives_while_she_speaks_is_echo():
    verdict = daemon.phone_utterance_decision(
        "claire what is the weather",
        kind="transcript",
        speaking=True,
        seconds_since_speech=0.0,
        wake_word="claire",
        aliases=[],
        fuzzy_ratio=0.82,
    )
    assert verdict == "echo"


def test_a_transcript_in_the_tail_after_she_stops_is_echo():
    # The phone's audio crosses the network after it was captured, so the last
    # words of her sentence can land just after ``is_speaking()`` goes False.
    verdict = daemon.phone_utterance_decision(
        "claire is my name",
        kind="transcript",
        speaking=False,
        seconds_since_speech=daemon.PHONE_ECHO_GRACE_SEC - 0.1,
        wake_word="claire",
        aliases=[],
        fuzzy_ratio=0.82,
    )
    assert verdict == "echo"


def test_a_transcript_long_after_she_stops_is_judged_normally():
    verdict = daemon.phone_utterance_decision(
        "klair are you there",
        kind="transcript",
        speaking=False,
        seconds_since_speech=daemon.PHONE_ECHO_GRACE_SEC + 5.0,
        wake_word="claire",
        aliases=[],
        fuzzy_ratio=0.82,
    )
    assert verdict == "answer"


def test_typed_text_is_addressed_by_construction():
    # /text never touched a microphone, so it cannot be her own voice returning,
    # and typing is as deliberate as typing into the laptop's chat window -- which
    # has never needed a wake word.
    for kind in ("text",):
        verdict = daemon.phone_utterance_decision(
            "what is the weather",
            kind=kind,
            speaking=True,
            seconds_since_speech=0.0,
            wake_word="claire",
            aliases=[],
            fuzzy_ratio=0.82,
        )
        assert verdict == "answer", kind


def test_a_daemon_with_no_wake_name_refuses_ambient_audio():
    # Misconfigured to the point of having no name: refusing is the honest
    # failure, because answering everything is precisely the defect.
    verdict = daemon.phone_utterance_decision(
        "anything at all",
        kind="transcript",
        speaking=False,
        seconds_since_speech=None,
        wake_word="",
        aliases=[],
        fuzzy_ratio=0.82,
    )
    assert verdict == "ambient"


# --------------------------------------------------------------------------- #
# the same decision, driven through a real socket
# --------------------------------------------------------------------------- #

def test_ambient_audio_over_a_real_socket_is_kept_and_not_answered(
    tmp_path, phone
):
    recogniser = FakeRecogniser("so anyway the kettle is still broken")
    b = make_bridge(tmp_path, sink=daemon._phone_utterance_sink, transcriber=recogniser)
    phone.monkeypatch.setattr(daemon, "_phone_bridge", b, raising=False)

    with live_bridge(b) as port:
        status, payload = post_audio(port, wav_bytes(), b.token())

    assert status == 200, payload
    assert payload["text"] == "so anyway the kettle is still broken"
    assert phone.answered == [], "the room was answered as if it had called her"
    assert wait_for(lambda: len(daemon._PHONE_AMBIENT) == 1)
    assert daemon._PHONE_AMBIENT[0] == "so anyway the kettle is still broken"


def test_audio_that_names_her_over_a_real_socket_is_answered(tmp_path, phone):
    recogniser = FakeRecogniser("claire what is the weather in lisbon")
    b = make_bridge(tmp_path, sink=daemon._phone_utterance_sink, transcriber=recogniser)
    phone.monkeypatch.setattr(daemon, "_phone_bridge", b, raising=False)

    with live_bridge(b) as port:
        status, payload = post_audio(port, wav_bytes(), b.token())

    assert status == 200, payload
    assert wait_for(lambda: len(phone.answered) == 1)
    assert phone.answered[0] == "claire what is the weather in lisbon"


def test_the_loop_is_broken_by_the_gate_itself(tmp_path, phone):
    """The defect, end to end: Claire hears her own answer and stops.

    This is the shape of the loop the owner hit.  Her reply is fed back in as
    ambient audio -- exactly what the phone's microphone does when it hears the
    phone's speaker -- and the assertion is that the reply engine is never
    entered a second time.  Before the gate, the second utterance was answered
    and this test would run away; the recogniser is driven by whatever was last
    fed to it, so the loop is explicit rather than implied.
    """
    her_own_answer = "it is twenty degrees and cloudy in lisbon today"
    recogniser = FakeRecogniser(her_own_answer)
    b = make_bridge(tmp_path, sink=daemon._phone_utterance_sink, transcriber=recogniser)
    phone.monkeypatch.setattr(daemon, "_phone_bridge", b, raising=False)

    with live_bridge(b) as port:
        first_status, _ = post_audio(port, wav_bytes(), b.token())
        # ...the phone's speaker plays her answer, its microphone picks it up, and
        # the "reply" that came out is transcribed as though the room said it.
        second_status, payload = post_audio(port, wav_bytes(), b.token())

    assert first_status == 200 and second_status == 200
    assert payload["text"] == her_own_answer
    assert phone.answered == [], "Claire answered her own sentence"


def test_audio_heard_while_she_speaks_is_dropped_over_a_real_socket(tmp_path, phone):
    recogniser = FakeRecogniser("claire tell me a story")
    b = make_bridge(tmp_path, sink=daemon._phone_utterance_sink, transcriber=recogniser)
    phone.monkeypatch.setattr(daemon, "_phone_bridge", b, raising=False)
    phone.monkeypatch.setattr(daemon, "_global_tts_engine", FakeTts(True), raising=False)

    with live_bridge(b) as port:
        status, _ = post_audio(port, wav_bytes(), b.token())

    assert status == 200
    assert phone.answered == []
    # Echo is not kept either: the room buffer must not fill with her own voice.
    assert list(daemon._PHONE_AMBIENT) == []


def test_typed_text_over_a_real_socket_is_answered_without_a_wake_word(tmp_path, phone):
    recogniser = FakeRecogniser("unused")
    b = make_bridge(tmp_path, sink=daemon._phone_utterance_sink, transcriber=recogniser)
    phone.monkeypatch.setattr(daemon, "_phone_bridge", b, raising=False)

    with live_bridge(b) as port:
        status, payload = post_text(port, "what is the weather", b.token())

    assert status == 202, payload
    assert wait_for(lambda: len(phone.answered) == 1)
    assert phone.answered[0] == "what is the weather"


# --------------------------------------------------------------------------- #
# the queue: a second sentence waits instead of being thrown away
# --------------------------------------------------------------------------- #

def test_queued_utterances_are_all_answered_in_order(phone):
    daemon._phone_enqueue("first question")
    daemon._phone_enqueue("second question")
    daemon._phone_enqueue("third question")

    assert wait_for(lambda: len(phone.answered) == 3)
    assert phone.answered == ["first question", "second question", "third question"]


def test_an_empty_utterance_is_not_queued(phone):
    daemon._phone_enqueue("")
    daemon._phone_enqueue("   ")
    time.sleep(0.2)
    assert phone.answered == []


def test_the_queue_is_bounded_rather_than_unbounded(phone, monkeypatch):
    # A hung engine must not grow the queue without limit.  The cap refuses the
    # newest rather than silently evicting the oldest, so what is already waiting
    # is never lost.
    monkeypatch.setattr(daemon, "_phone_dispatching", True, raising=False)
    with daemon._phone_lock:
        daemon._phone_queue.clear()
    for index in range(daemon.PHONE_QUEUE_MAX + 5):
        daemon._phone_enqueue(f"question {index}")
    with daemon._phone_lock:
        assert len(daemon._phone_queue) == daemon.PHONE_QUEUE_MAX
        assert daemon._phone_queue[0] == "question 0"
    with daemon._phone_lock:
        daemon._phone_queue.clear()
        daemon._phone_dispatching = False


# --------------------------------------------------------------------------- #
# submit_text_query: wait for the lock when told to, keep rejecting otherwise
# --------------------------------------------------------------------------- #

class _Recorder:
    """Captures the callback events submit_text_query emits."""

    def __init__(self) -> None:
        self.events = []
        self.done = threading.Event()

    def complete(self, reply):
        self.events.append(("complete", reply))
        self.done.set()

    def busy(self):
        self.events.append(("busy", None))


@pytest.fixture()
def engine(monkeypatch):
    """A stubbed reply engine and the three globals submit_text_query needs."""
    calls = []

    def fake_run_reply_engine(*, db, cfg, tts, text, dialogue_memory, language, quiet):
        calls.append(text)
        time.sleep(0.05)
        return f"answer to {text}"

    import claire.reply.engine as engine_module

    monkeypatch.setattr(engine_module, "run_reply_engine", fake_run_reply_engine)
    monkeypatch.setattr(daemon, "_global_dialogue_memory", object(), raising=False)
    monkeypatch.setattr(daemon, "_global_cfg", object(), raising=False)
    monkeypatch.setattr(daemon, "_global_db", object(), raising=False)
    monkeypatch.setattr(daemon, "_global_stop_requested", False, raising=False)
    return calls


def test_wait_until_free_waits_for_a_running_query_instead_of_refusing(engine):
    recorder = _Recorder()
    daemon._chat_query_lock.acquire()
    try:
        worker = threading.Thread(
            target=lambda: daemon.submit_text_query(
                "the queued question",
                on_complete=recorder.complete,
                on_busy=recorder.busy,
                wait_until_free=True,
            ),
            daemon=True,
        )
        worker.start()
        # It is blocked on the lock, and crucially it has NOT reported busy.
        time.sleep(0.2)
        assert recorder.events == []
        daemon._chat_query_lock.release()
        worker.join(timeout=5)
        assert recorder.done.wait(timeout=5)
    finally:
        if daemon._chat_query_lock.locked():
            daemon._chat_query_lock.release()
    assert ("busy", None) not in recorder.events
    assert recorder.events[-1] == ("complete", "answer to the queued question")
    assert engine == ["the queued question"]


def test_without_the_flag_a_second_submission_is_still_rejected_with_busy(engine):
    # The chat window keeps this behaviour: there a person can see the "busy".
    recorder = _Recorder()
    daemon._chat_query_lock.acquire()
    try:
        daemon.submit_text_query(
            "rejected while busy",
            on_complete=recorder.complete,
            on_busy=recorder.busy,
        )
    finally:
        daemon._chat_query_lock.release()
    assert recorder.events == [("busy", None)]
    assert engine == []
