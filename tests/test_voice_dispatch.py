"""A voice query must actually reach the reply engine.

``_dispatch_query`` used to capture the capture generation *before* calling
``_clear_audio_buffers()``, which bumps that same generation. Its staleness
guard therefore rejected every voice turn, and because the guard's only trace
was a ``debug_log`` line it failed completely silently: the wake word matched,
the thinking tune played, "Working on it" printed, and then no reply was ever
generated or spoken. Text queries were unaffected because they do not go
through this method, which is why the defect looked like "voice is broken but
typing works".

These tests also pin the protection the guard exists for: a turn superseded
while its reply was being generated must still be discarded rather than spoken.
"""

import queue
import threading
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from claire.listening.listener import VoiceListener

pytestmark = pytest.mark.unit


class FakeTTS:
    """Records what would have been spoken without touching audio hardware."""

    enabled = True

    def __init__(self, speaking=False):
        self.spoken = []
        self.speaking = speaking

    def speak(self, text, **kwargs):
        self.spoken.append(text)

    def is_speaking(self):
        return self.speaking


def dispatch_listener(tts=None, **cfg_overrides):
    obj = VoiceListener.__new__(VoiceListener)
    obj.cfg = SimpleNamespace(voice_debug=False, **cfg_overrides)
    obj.db = None
    obj.dialogue_memory = None
    obj.tts = tts
    obj._last_detected_language = None
    obj._should_stop = False
    obj._audio_state_lock = threading.RLock()
    obj._audio_generation = 0
    obj._capture_generation = 0
    obj._utterance_frames = []
    obj._utterance_voice_frames = 0
    obj._utterance_speech_rms_sum = 0.0
    obj._pre_roll = deque()
    obj._pending_audio = None
    obj.is_speech_active = False
    obj._silence_frames = 0
    obj._wake_timestamp = None
    obj._streaming_partial_text = ""
    obj._streaming_segment_started_at = None
    obj._streaming_stt = None
    obj._audio_q = queue.Queue()
    obj.tune_stops = 0
    obj._stop_thinking_tune = lambda: setattr(obj, "tune_stops", obj.tune_stops + 1)
    obj.track_tts_start = lambda reply: None
    return obj


def test_voice_dispatch_reaches_the_reply_engine(monkeypatch):
    calls = []
    monkeypatch.setattr("claire.reply.engine.run_reply_engine",
                        lambda *a, **k: calls.append(a) or "Sunny tomorrow.")
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    obj = dispatch_listener()
    obj._dispatch_query("what's the weather tomorrow")

    assert calls, "the voice turn must reach the reply engine"


def test_voice_dispatch_speaks_the_reply(monkeypatch):
    monkeypatch.setattr("claire.reply.engine.run_reply_engine",
                        lambda *a, **k: "Sunny tomorrow.")
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    tts = FakeTTS()
    obj = dispatch_listener(tts, ack_phrases=["On it."])
    obj._dispatch_query("what's the weather tomorrow")

    assert tts.spoken == ["On it.", "Sunny tomorrow."]


def test_reply_is_discarded_when_a_new_turn_supersedes_it(monkeypatch):
    # The session boundary the guard protects: a new turn clears the audio
    # buffers while this reply is still being generated, so it must not be
    # spoken into the new turn. The acknowledgement was already spoken by then,
    # which is honest — she *was* working on it — but the stale answer is not.
    def superseded_by(*_args, **_kwargs):
        obj._clear_audio_buffers()
        return "Stale answer."

    monkeypatch.setattr("claire.reply.engine.run_reply_engine", superseded_by)
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    tts = FakeTTS()
    obj = dispatch_listener(tts)
    obj._dispatch_query("what's the weather tomorrow")

    assert "Stale answer." not in tts.spoken


def test_dispatch_after_shutdown_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr("claire.reply.engine.run_reply_engine",
                        lambda *a, **k: called.append(a) or "Nope.")
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    obj = dispatch_listener()
    obj._should_stop = True
    obj._dispatch_query("what's the weather tomorrow")

    assert called == []


def test_acknowledgement_is_spoken_before_the_reply_engine(monkeypatch):
    """She answers back first, then does the work.

    The order is the whole point: the point of the acknowledgement is that it
    is heard *while* the reply engine is still thinking, so a test that only
    checks both were spoken would pass even if the ack came last.
    """
    order = []

    def engine(*_args, **_kwargs):
        order.append("engine")
        return "Sunny tomorrow."

    monkeypatch.setattr("claire.reply.engine.run_reply_engine", engine)
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    class OrderedTTS(FakeTTS):
        def speak(self, text, **kwargs):
            order.append(f"speak:{text}")
            super().speak(text, **kwargs)

    obj = dispatch_listener(OrderedTTS(), ack_phrases=["On it."])
    obj._dispatch_query("what's the weather tomorrow")

    assert order == ["speak:On it.", "engine", "speak:Sunny tomorrow."]


def test_acknowledgement_stops_the_thinking_tune(monkeypatch):
    """The tune must not play under the voice saying the acknowledgement."""
    tune_stops_when_engine_ran = []

    def engine(*_args, **_kwargs):
        tune_stops_when_engine_ran.append(obj.tune_stops)
        return "Sunny tomorrow."

    monkeypatch.setattr("claire.reply.engine.run_reply_engine", engine)
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    obj = dispatch_listener(FakeTTS(), ack_phrases=["On it."])
    obj._dispatch_query("what's the weather tomorrow")

    assert tune_stops_when_engine_ran == [1], "the ack must stop the tune first"


def test_acknowledgement_can_be_switched_off(monkeypatch):
    monkeypatch.setattr("claire.reply.engine.run_reply_engine",
                        lambda *a, **k: "Sunny tomorrow.")
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    tts = FakeTTS()
    obj = dispatch_listener(tts, ack_enabled=False, ack_phrases=["On it."])
    obj._dispatch_query("what's the weather tomorrow")

    assert tts.spoken == ["Sunny tomorrow."]


def test_acknowledgement_is_skipped_while_she_is_already_speaking(monkeypatch):
    """Talking over a reply must not queue a late "on it" behind it."""
    monkeypatch.setattr("claire.reply.engine.run_reply_engine",
                        lambda *a, **k: "Sunny tomorrow.")
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    tts = FakeTTS(speaking=True)
    obj = dispatch_listener(tts, ack_phrases=["On it."])
    obj._dispatch_query("wait, what's the weather tomorrow")

    assert tts.spoken == ["Sunny tomorrow."]


def test_acknowledgement_phrases_rotate(monkeypatch):
    monkeypatch.setattr("claire.reply.engine.run_reply_engine",
                        lambda *a, **k: "Sunny tomorrow.")
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    tts = FakeTTS()
    obj = dispatch_listener(tts, ack_phrases=["On it.", "Right away."])
    for _ in range(3):
        obj._dispatch_query("what's the weather tomorrow")

    assert [t for t in tts.spoken if t != "Sunny tomorrow."] == [
        "On it.", "Right away.", "On it.",
    ]


def test_acknowledgement_is_not_spoken_after_shutdown(monkeypatch):
    monkeypatch.setattr("claire.reply.engine.run_reply_engine",
                        lambda *a, **k: "Sunny tomorrow.")
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    tts = FakeTTS()
    obj = dispatch_listener(tts, ack_phrases=["On it."])
    obj._should_stop = True
    obj._dispatch_query("what's the weather tomorrow")

    assert tts.spoken == []
