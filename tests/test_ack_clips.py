"""Behavioural tests for the pre-recorded acknowledgement and its continuations.

The acknowledgement is the first thing a person hears after speaking. These
tests pin:

- a cached clip is preferred over live synthesis, and synthesis is the
  fallback whenever no clip can be played;
- every existing guard still holds (off switch, already speaking, stale turn);
- the phrase rotation still advances;
- continuations fire at the configured marks, differ from the acknowledgement
  and from each other, and stop the instant the answer arrives or the turn is
  abandoned.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from types import SimpleNamespace
import numpy as np
import pytest

from claire.listening.ack_continuations import (
    ContinuationSchedule,
    pick_least_recently_used,
)
from claire.listening.listener import VoiceListener
from claire.output.clip_cache import Clip

pytestmark = pytest.mark.unit


def make_clip(phrase, seconds=0.02, rate=22050):
    return Clip(
        phrase=phrase,
        samples=np.zeros(int(rate * seconds), dtype=np.int16),
        sample_rate=rate,
    )


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


class RecordingPlayer:
    """Stands in for ClipPlayer, recording phrases and honouring is_speaking."""

    def __init__(self):
        self.played = []
        self.speaking = False
        self.sticky_speaking = False
        self.stop_calls = 0

    def play(self, clip, on_finished=None):
        self.played.append(clip.phrase)
        # A real clip is asynchronous; a test clip is treated as already
        # finished unless the test holds ``speaking`` true itself.
        self.speaking = self.sticky_speaking
        return True

    def stop(self, **kwargs):
        self.stop_calls += 1
        self.speaking = False

    def is_speaking(self):
        return self.speaking


class RecordingLibrary:
    """Serves clips from a dict and records prewarm requests."""

    def __init__(self, phrases=()):
        self.clips = {phrase: make_clip(phrase) for phrase in phrases}
        self.prewarm_requests = []

    def get(self, phrase):
        return self.clips.get(phrase)

    def prewarm(self, phrases):
        self.prewarm_requests.extend(phrases)
        return None

    def available(self, phrases):
        return [self.clips[p] for p in phrases if p in self.clips]


def ack_listener(tts=None, *, marks=(0.05, 0.15), library_phrases=("On it.", "Okay."),
                 player=None, library=None, **cfg_overrides):
    obj = VoiceListener.__new__(VoiceListener)
    cfg = dict(
        voice_debug=False,
        ack_enabled=True,
        ack_clips_enabled=True,
        ack_phrases=["On it.", "Okay."],
        ack_continuation_phrases=["Still on it.", "Nearly there.", "Just a moment."],
        tune_enabled=False,
    )
    cfg.update(cfg_overrides)
    obj.cfg = SimpleNamespace(**cfg)
    obj.db = None
    obj.dialogue_memory = None
    obj.tts = tts
    obj._last_detected_language = None
    obj._should_stop = False
    obj._audio_state_lock = threading.RLock()
    obj._audio_generation = 0
    obj._capture_generation = 0
    obj._recent_audio_energy = deque()
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
    obj._ack_index = 0
    obj._clip_library = library if library is not None else RecordingLibrary(library_phrases)
    obj._clip_player = player if player is not None else RecordingPlayer()
    obj._continuation_lru = None
    obj._continuation_spoken = set()
    obj._continuation_lock = threading.Lock()
    obj._continuation_schedule = None
    obj._continuation_marks = marks
    obj.tune_stops = 0
    obj._stop_thinking_tune = lambda: setattr(obj, "tune_stops", obj.tune_stops + 1)
    obj.tts_starts = []
    obj.track_tts_start = lambda text: obj.tts_starts.append(text)
    return obj


# --- The acknowledgement prefers a clip ------------------------------------


def test_acknowledgement_plays_a_clip_instead_of_synthesising():
    tts = FakeTTS()
    player = RecordingPlayer()
    obj = ack_listener(tts, player=player)

    obj._speak_acknowledgement(obj._capture_generation)
    obj._cancel_ack_audio()

    assert player.played == ["On it."]
    assert tts.spoken == [], "the clip replaces synthesis, it does not accompany it"


def test_acknowledgement_falls_back_to_synthesis_without_a_clip():
    tts = FakeTTS()
    player = RecordingPlayer()
    obj = ack_listener(tts, player=player, library_phrases=())

    obj._speak_acknowledgement(obj._capture_generation)
    obj._cancel_ack_audio()

    assert tts.spoken == ["On it."]
    assert player.played == []


def test_acknowledgement_falls_back_when_the_player_refuses(monkeypatch):
    tts = FakeTTS()
    player = RecordingPlayer()
    monkeypatch.setattr(player, "play", lambda clip, on_finished=None: False)
    obj = ack_listener(tts, player=player)

    obj._speak_acknowledgement(obj._capture_generation)
    obj._cancel_ack_audio()

    assert tts.spoken == ["On it."]


def test_acknowledgement_requests_a_missing_clip_without_blocking():
    tts = FakeTTS()
    library = RecordingLibrary(phrases=())
    obj = ack_listener(tts, library=library)

    obj._speak_acknowledgement(obj._capture_generation)
    obj._cancel_ack_audio()

    assert library.prewarm_requests == ["On it."]
    assert tts.spoken == ["On it."]


def test_acknowledgement_still_rotates_phrases():
    tts = FakeTTS()
    player = RecordingPlayer()
    obj = ack_listener(tts, player=player, library_phrases=("On it.", "Okay."))

    obj._speak_acknowledgement(obj._capture_generation)
    obj._cancel_ack_audio()
    obj._speak_acknowledgement(obj._capture_generation)
    obj._cancel_ack_audio()

    assert player.played == ["On it.", "Okay."]


def test_acknowledgement_is_skipped_while_she_is_already_speaking():
    tts = FakeTTS(speaking=True)
    player = RecordingPlayer()
    obj = ack_listener(tts, player=player)

    obj._speak_acknowledgement(obj._capture_generation)

    assert player.played == []
    assert tts.spoken == []


def test_acknowledgement_is_skipped_for_a_stale_turn():
    tts = FakeTTS()
    player = RecordingPlayer()
    obj = ack_listener(tts, player=player)

    obj._speak_acknowledgement(obj._capture_generation + 1)

    assert player.played == []
    assert tts.spoken == []


def test_acknowledgement_is_skipped_when_disabled():
    tts = FakeTTS()
    player = RecordingPlayer()
    obj = ack_listener(tts, player=player, ack_enabled=False)

    obj._speak_acknowledgement(obj._capture_generation)

    assert player.played == []
    assert tts.spoken == []


def test_clips_can_be_switched_off_independently():
    tts = FakeTTS()
    player = RecordingPlayer()
    obj = ack_listener(tts, player=player, ack_clips_enabled=False)

    obj._speak_acknowledgement(obj._capture_generation)

    assert player.played == []
    assert tts.spoken == ["On it."]


def test_acknowledgement_still_stops_the_thinking_tune():
    tts = FakeTTS()
    obj = ack_listener(tts)

    obj._speak_acknowledgement(obj._capture_generation)
    obj._cancel_ack_audio()

    assert obj.tune_stops == 1


def test_no_clip_layer_at_all_still_synthesises():
    """Listener objects built without the clip layer must keep working."""
    tts = FakeTTS()
    obj = ack_listener(tts)
    obj._clip_library = None
    obj._clip_player = None

    obj._speak_acknowledgement(obj._capture_generation)

    assert tts.spoken == ["On it."]


# --- Continuations ----------------------------------------------------------


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_continuation_plays_after_the_mark():
    tts = FakeTTS()
    player = RecordingPlayer()
    library = RecordingLibrary(phrases=["On it.", "Still on it.", "Nearly there.", "Just a moment."])
    obj = ack_listener(tts, player=player, library=library, marks=(0.05,))

    started = time.time()
    obj._speak_acknowledgement(obj._capture_generation)
    assert _wait_for(lambda: len(player.played) >= 2, timeout=2.0)
    elapsed = time.time() - started
    obj._cancel_ack_audio()

    assert player.played[0] == "On it."
    assert len(player.played) == 2
    assert elapsed >= 0.05


def test_continuations_are_least_recently_used_and_never_repeat_in_a_turn():
    tts = FakeTTS()
    player = RecordingPlayer()
    library = RecordingLibrary(phrases=["Still on it.", "Nearly there.", "Just a moment."])
    obj = ack_listener(tts, player=player, library=library)

    for _ in range(4):
        obj._play_continuation_clip(obj._capture_generation)

    assert player.played[:3] == ["Still on it.", "Nearly there.", "Just a moment."]
    assert player.played[3] == "Still on it.", "the least recently used phrase comes round"


def test_continuation_never_plays_over_the_answer():
    tts = FakeTTS()
    player = RecordingPlayer()
    library = RecordingLibrary(phrases=["On it.", "Still on it.", "Nearly there."])
    obj = ack_listener(tts, player=player, library=library, marks=(0.2, 0.3))

    obj._speak_acknowledgement(obj._capture_generation)
    time.sleep(0.02)
    obj._cancel_ack_audio()  # the answer is ready
    time.sleep(0.3)

    assert player.played == ["On it."], "a continuation must not follow the answer"


def test_continuation_never_plays_while_tts_is_speaking():
    tts = FakeTTS(speaking=True)
    player = RecordingPlayer()
    library = RecordingLibrary(phrases=["Still on it."])
    obj = ack_listener(tts, player=player, library=library)

    obj._play_continuation_clip(obj._capture_generation)

    assert player.played == []


def test_continuation_skips_a_phrase_used_by_the_acknowledgement():
    tts = FakeTTS()
    player = RecordingPlayer()
    library = RecordingLibrary(phrases=["On it.", "Still on it."])
    obj = ack_listener(
        tts,
        player=player,
        library=library,
        ack_continuation_phrases=["On it.", "Still on it."],
    )

    obj._speak_acknowledgement(obj._capture_generation)
    obj._play_continuation_clip(obj._capture_generation)

    assert player.played == ["On it.", "Still on it."]


def test_continuation_is_skipped_once_the_turn_goes_stale():
    tts = FakeTTS()
    player = RecordingPlayer()
    library = RecordingLibrary(phrases=["Still on it."])
    obj = ack_listener(tts, player=player, library=library)

    obj._capture_generation += 1
    obj._play_continuation_clip(obj._capture_generation - 1)

    assert player.played == []


def test_cancelling_stops_the_clip_player():
    tts = FakeTTS()
    player = RecordingPlayer()
    player.speaking = True
    obj = ack_listener(tts, player=player)

    obj._cancel_ack_audio()

    assert player.stop_calls >= 1
    assert player.is_speaking() is False


def test_cancel_is_safe_when_the_clip_layer_is_absent():
    tts = FakeTTS()
    obj = ack_listener(tts)
    obj._clip_library = None
    obj._clip_player = None

    obj._cancel_ack_audio()
    obj._speak_acknowledgement(obj._capture_generation)


def test_abandoning_a_turn_cancels_continuations():
    tts = FakeTTS()
    player = RecordingPlayer()
    library = RecordingLibrary(phrases=["On it.", "Still on it."])
    obj = ack_listener(tts, player=player, library=library, marks=(0.2,))

    obj._speak_acknowledgement(obj._capture_generation)
    obj._clear_audio_buffers()  # a new utterance / barge-in takes over
    time.sleep(0.3)

    assert player.played == ["On it."]


# --- The schedule itself ----------------------------------------------------


def test_schedule_plays_at_every_mark_that_elapses():
    played = []
    schedule = ContinuationSchedule((0.0, 0.02, 0.04), lambda: played.append(time.time()))

    schedule.start()
    assert _wait_for(lambda: len(played) >= 3, timeout=2.0)
    schedule.cancel()

    assert len(played) == 3
    assert played[1] - played[0] >= 0.015


def test_schedule_cancel_stops_every_future_play():
    played = []
    schedule = ContinuationSchedule((0.1, 0.2), lambda: played.append(1))

    schedule.start()
    time.sleep(0.02)
    schedule.cancel()

    time.sleep(0.3)
    assert played == []
    assert not schedule.is_running()


def test_schedule_keeps_going_when_a_play_raises():
    played = []

    def play():
        played.append(1)
        raise RuntimeError("device busy")

    schedule = ContinuationSchedule((0.0, 0.02), play)
    schedule.start()
    assert _wait_for(lambda: len(played) >= 2, timeout=2.0)
    schedule.cancel()

    assert len(played) == 2


def test_schedule_does_nothing_without_marks():
    played = []
    schedule = ContinuationSchedule((), lambda: played.append(1))

    schedule.start()
    schedule.cancel()

    assert played == []


def test_least_recently_used_order_advances():
    order = ["a", "b", "c"]
    first = pick_least_recently_used(order)
    order.remove(first)
    order.append(first)
    assert pick_least_recently_used(order) == "b"
