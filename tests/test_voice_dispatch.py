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

    def __init__(self):
        self.spoken = []

    def speak(self, text, **kwargs):
        self.spoken.append(text)

    def is_speaking(self):
        return False


def dispatch_listener(tts=None):
    obj = VoiceListener.__new__(VoiceListener)
    obj.cfg = SimpleNamespace(voice_debug=False)
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
    obj = dispatch_listener(tts)
    obj._dispatch_query("what's the weather tomorrow")

    assert tts.spoken == ["Sunny tomorrow."]


def test_reply_is_discarded_when_a_new_turn_supersedes_it(monkeypatch):
    # The session boundary the guard protects: a new turn clears the audio
    # buffers while this reply is still being generated, so it must not be
    # spoken into the new turn.
    def superseded_by(*_args, **_kwargs):
        obj._clear_audio_buffers()
        return "Stale answer."

    monkeypatch.setattr("claire.reply.engine.run_reply_engine", superseded_by)
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    tts = FakeTTS()
    obj = dispatch_listener(tts)
    obj._dispatch_query("what's the weather tomorrow")

    assert tts.spoken == []


def test_dispatch_after_shutdown_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr("claire.reply.engine.run_reply_engine",
                        lambda *a, **k: called.append(a) or "Nope.")
    monkeypatch.setattr("claire.daemon.query_lock", nullcontext)

    obj = dispatch_listener()
    obj._should_stop = True
    obj._dispatch_query("what's the weather tomorrow")

    assert called == []
