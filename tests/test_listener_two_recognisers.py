"""The listener keeps two recognisers honest (D3/D4).

The streaming recogniser feeds wake detection with provisional text; the
authoritative engine produces the text the turn acts on; and when no
authoritative model exists the turn is marked degraded rather than silently
passing a guess off as a verified transcript.

No audio device and no real model: both factories are replaced with fakes.
"""

import queue
import threading
import time
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import numpy as np
import pytest

from claire.config import load_settings
from claire.listening.listener import (
    VoiceListener,
    _CapturedAudio,
    _TranscriptionResult,
)
from claire.listening.stt import SttResult
from claire.listening.transcript import SOURCE_STREAMING_FALLBACK
import claire.listening.listener as capture

pytestmark = pytest.mark.unit

STREAMING_TEXT = "claire tell me a joke"
AUTHORITATIVE_TEXT = "Claire set a timer for ten minutes"


class FakeStreamingEngine:
    """Stands in for the sherpa-onnx always-on recogniser."""

    name = "fake_streaming"

    def __init__(self, text=STREAMING_TEXT, fail_on_accept=False):
        self.text = text
        self.fail_on_accept = fail_on_accept
        self.accepted = []
        self.partial_calls = 0
        self.finalized = 0
        self.resets = 0

    def accept_waveform(self, samples, sample_rate):
        if self.fail_on_accept:
            raise RuntimeError("synthetic recognizer failure")
        self.accepted.append((len(samples), sample_rate))

    def partial_text(self):
        self.partial_calls += 1
        return self.text

    def finalize(self):
        self.finalized += 1
        return self.text

    def reset(self):
        self.resets += 1


class FakeAuthoritativeEngine:
    """Stands in for the Parakeet engine that transcribes the turn."""

    name = "parakeet_nemo"

    def __init__(self, text=AUTHORITATIVE_TEXT):
        self.text = text
        self.calls = []

    def transcribe(self, audio, sample_rate):
        self.calls.append((len(audio), sample_rate))
        return SttResult(text=self.text, language="en", engine=self.name)

    def identity(self):
        return SimpleNamespace(engine=self.name, model_path="/fake/model.nemo")


@pytest.fixture
def listener(monkeypatch):
    """A listener with the real __init__, no model and no audio device."""
    cfg = replace(load_settings(), voice_debug=False, vad_enabled=False,
                  whisper_backend='faster-whisper', whisper_device='cpu')
    monkeypatch.setattr(capture, 'create_intent_judge', lambda _cfg: None)
    monkeypatch.setattr(capture, 'FASTER_WHISPER_AVAILABLE', True)
    monkeypatch.setattr(capture, '_load_faster_whisper_model', lambda *a, **kw: MagicMock())
    monkeypatch.setattr(VoiceListener, '_decode_faster_whisper', lambda *a: ([], None))
    monkeypatch.setattr(VoiceListener, '_start_llm_warmup', lambda _self: [])
    monkeypatch.setattr('desktop_app.face_widget.get_claire_state', lambda: MagicMock())
    obj = VoiceListener(MagicMock(), cfg, None, None)
    yield obj
    obj.stop()


def inject_engines(monkeypatch, authoritative=None, streaming=...):
    """Point the listener's lazy factories at fakes.

    Passing ``streaming=None`` is the "streaming not configured" case and must
    leave today's behaviour untouched.
    """
    if authoritative is not None:
        monkeypatch.setattr('claire.listening.stt.create_stt_engine',
                            lambda _cfg: authoritative)
    if streaming is not ...:
        monkeypatch.setattr('claire.listening.stt.create_streaming_stt_engine',
                            lambda _cfg: streaming)
    return authoritative, streaming


def prime_utterance(obj, frames=8):
    """Give the listener a finished utterance so _finalize_utterance queues it."""
    frame = np.full(int(obj._samplerate * 0.02), 0.1, dtype=np.float32)
    obj._utterance_frames = [frame.copy() for _ in range(frames)]
    obj.is_speech_active = True
    obj.echo_detector._utterance_start_time = time.time() - 0.2
    obj._capture_generation = getattr(obj, "_capture_generation", 0)


def run_one_job(obj, timeout=5.0):
    """Run the serial transcription worker until it has produced one result."""
    worker = threading.Thread(target=obj._run_transcription_worker, daemon=True)
    worker.start()
    try:
        return obj._transcription_results_q.get(timeout=timeout)
    finally:
        obj._transcription_jobs_q.put(None)
        worker.join(2)


def record_transcripts(obj):
    """Capture what the turn handler is handed, without running the handler."""
    seen = []

    def spy(text, *args, **kwargs):
        seen.append((text, kwargs.get('provisional_text', '')))

    obj._process_transcript = spy
    return seen


# -- honest degradation ----------------------------------------------------

def test_unset_stt_model_path_degrades_the_turn_and_names_the_setting(listener):
    obj = listener
    assert not obj.cfg.stt_model_path, "this test needs stt_model_path to be unset"
    seen = record_transcripts(obj)
    obj._transcribe_audio = lambda audio: ("Claire what is the weather", "en", ())

    prime_utterance(obj)
    obj._finalize_utterance()
    result = run_one_job(obj)
    obj._handle_transcription_result(result)

    assert result.turn is not None
    assert result.turn.degraded is True
    assert result.turn.source == SOURCE_STREAMING_FALLBACK
    assert "stt_model_path" in result.turn.degraded_reason
    # The existing Whisper path still answered, and its text is what acts.
    assert result.text == "Claire what is the weather"
    assert seen == [("Claire what is the weather", "")]


def test_degradation_is_logged_once_in_the_voice_log(listener, monkeypatch):
    obj = listener
    logged = []
    monkeypatch.setattr(capture, 'debug_log', lambda message, *a, **kw: logged.append(message))
    obj._transcribe_audio = lambda audio: ("Claire what is the weather", "en", ())

    first = obj._decode_utterance(np.zeros(1600, dtype=np.float32))
    second = obj._decode_utterance(np.zeros(1600, dtype=np.float32))

    assert first[3] == SOURCE_STREAMING_FALLBACK
    assert second[3] == SOURCE_STREAMING_FALLBACK
    degraded = [m for m in logged if 'degraded' in m]
    assert len(degraded) == 1, f"degradation must be visible but not repeated: {logged}"
    assert 'stt_model_path' in degraded[0]


def test_missing_engine_never_crashes_the_listener(listener):
    obj = listener
    # The real factory raises SttModelMissingError for an unset path; the
    # listener must swallow it, record the reason and stay usable.
    assert obj._get_authoritative_stt_engine() is None
    assert 'stt_model_path' in obj._authoritative_stt_reason
    obj._transcribe_audio = lambda audio: ("Claire hello", "en", ())
    text, language, _events, source, reason = obj._decode_utterance(
        np.zeros(1600, dtype=np.float32)
    )
    assert (text, language, source) == ("Claire hello", "en", SOURCE_STREAMING_FALLBACK)
    assert 'stt_model_path' in reason


def test_named_model_error_with_a_path_set_still_degrades(listener):
    obj = listener
    obj.cfg = replace(obj.cfg, stt_model_path='/nonexistent/parakeet.nemo')
    obj._transcribe_audio = lambda audio: ('Claire hello', 'en', ())

    text, _language, _events, source, reason = obj._decode_utterance(
        np.zeros(1600, dtype=np.float32)
    )

    assert (text, source) == ('Claire hello', SOURCE_STREAMING_FALLBACK)
    assert '/nonexistent/parakeet.nemo' in reason


# -- the authoritative transcript is the one acted on ----------------------

def test_authoritative_engine_text_is_what_the_turn_acts_on(listener, monkeypatch):
    obj = listener
    authoritative = FakeAuthoritativeEngine(AUTHORITATIVE_TEXT)
    streaming = FakeStreamingEngine(STREAMING_TEXT)
    inject_engines(monkeypatch, authoritative, streaming)
    obj._transcribe_audio = Mock(side_effect=AssertionError('Whisper must not run'))

    prime_utterance(obj)
    obj._feed_streaming_stt(np.zeros(int(obj._samplerate * 0.02), dtype=np.float32))
    obj._finalize_utterance()
    seen = record_transcripts(obj)
    result = run_one_job(obj)
    obj._handle_transcription_result(result)

    assert authoritative.calls, "the authoritative engine must transcribe the utterance"
    assert obj._transcribe_audio.call_count == 0
    assert result.turn.source == 'parakeet_nemo'
    assert result.turn.degraded is False
    assert result.turn.divergent is True
    assert result.turn.provisional_text == STREAMING_TEXT
    # Real timings, relative to each other.
    assert result.turn.provisional_started_at is not None
    assert result.turn.provisional_finalized_at >= result.turn.provisional_started_at
    assert result.turn.authoritative_started_at is not None
    assert result.turn.authoritative_finalized_at >= result.turn.authoritative_started_at
    assert seen == [(AUTHORITATIVE_TEXT, STREAMING_TEXT)]
    assert 'joke' not in seen[0][0], "streaming text reached the turn handler"


def test_divergent_turn_never_hands_streaming_text_downstream(listener):
    obj = listener
    # A caller that put the streaming text in `text` must not be able to make
    # the turn act on it: the handler reads the turn's authoritative accessor.
    turn = obj._assembler().build(STREAMING_TEXT, AUTHORITATIVE_TEXT, 'parakeet_nemo')
    seen = record_transcripts(obj)
    result = _TranscriptionResult(
        text=STREAMING_TEXT,
        language='en',
        low_confidence_events=(),
        start_time=1.0,
        end_time=2.0,
        energy=0.1,
        capture_generation=obj._capture_generation,
        captured_during_tts=False,
        captured_tts_start_time=0.0,
        turn=turn,
        provisional_text=STREAMING_TEXT,
    )

    obj._handle_transcription_result(result)

    assert seen == [(AUTHORITATIVE_TEXT, STREAMING_TEXT)]
    assert 'joke' not in seen[0][0]


# -- streaming unavailable: today's behaviour, exactly ---------------------

def test_streaming_engine_unavailable_keeps_today_behaviour(listener, monkeypatch):
    obj = listener
    inject_engines(monkeypatch, FakeAuthoritativeEngine(AUTHORITATIVE_TEXT), None)

    assert obj._get_streaming_stt_engine() is None
    obj._feed_streaming_stt(np.zeros(1600, dtype=np.float32))  # no-op, no crash
    assert obj._finalize_streaming_segment() == ''

    prime_utterance(obj)
    obj._finalize_utterance()
    job = obj._transcription_jobs_q.get_nowait()
    assert job.provisional_text == ''
    obj._transcription_jobs_q.put(job)

    seen = record_transcripts(obj)
    result = run_one_job(obj)
    obj._handle_transcription_result(result)
    assert result.provisional_text == ''
    assert seen == [(AUTHORITATIVE_TEXT, '')]


def test_streaming_engine_unavailable_call_signature_is_unchanged(listener, monkeypatch):
    obj = listener
    inject_engines(monkeypatch, None, None)
    spy = Mock()
    obj._process_transcript = spy
    result = _TranscriptionResult(
        text='Claire weather', language='en', low_confidence_events=(),
        start_time=10.0, end_time=11.0, energy=0.2,
        capture_generation=obj._capture_generation,
        captured_during_tts=False, captured_tts_start_time=0.0,
        turn=obj._assembler().build('', 'Claire weather', 'parakeet_nemo'),
        provisional_text='',
    )

    obj._handle_transcription_result(result)

    spy.assert_called_once_with(
        'Claire weather', 0.2, 10.0, 11.0,
        generation=obj._capture_generation,
        captured_during_tts=False, captured_tts_start_time=0.0,
    )


def test_listener_still_wakes_and_transcribes_without_streaming(listener, monkeypatch):
    obj = listener
    inject_engines(monkeypatch, None, None)
    obj._transcribe_audio = lambda audio: ('Claire weather', 'en', ())

    assert obj._get_streaming_stt_engine() is None

    prime_utterance(obj)
    obj._finalize_utterance()
    result = run_one_job(obj)
    obj._handle_transcription_result(result)
    assert result.text == 'Claire weather'

    # Wake detection is unchanged when there is no streaming recogniser.
    obj._process_transcript(
        'Claire weather', captured_during_tts=False,
        captured_tts_start_time=0.0, generation=obj._capture_generation,
    )
    assert obj.state_manager.get_pending_query() == 'weather'


# -- feeding the streaming recogniser --------------------------------------

def test_capture_feeds_every_frame_to_the_streaming_recogniser(listener, monkeypatch):
    """The capture loop itself, not just the helper, must feed the streamer."""
    obj = listener
    streaming = FakeStreamingEngine('claire')
    inject_engines(monkeypatch, None, streaming)
    obj._should_stop = False
    obj._audio_q = queue.Queue()
    obj._vad = None
    obj._recent_audio_energy = deque(maxlen=20)
    obj._frame_samples = int(obj._samplerate * 0.02)
    frame = np.full(obj._frame_samples, 0.05, dtype=np.float32)

    worker = threading.Thread(target=obj._consume_audio_frames, args=(20,), daemon=True)
    worker.start()
    try:
        for _ in range(4):
            obj._audio_q.put(
                _CapturedAudio(audio=frame.copy(), generation=obj._audio_generation)
            )
        deadline = time.monotonic() + 3.0
        while len(streaming.accepted) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        obj._should_stop = True
        worker.join(2)

    assert len(streaming.accepted) >= 4
    assert all(rate == obj._samplerate for _length, rate in streaming.accepted)
    assert obj._provisional_text() == 'claire'


def test_streaming_frames_go_only_through_the_streaming_api(listener, monkeypatch):
    obj = listener
    streaming = FakeStreamingEngine('claire')
    inject_engines(monkeypatch, None, streaming)
    obj._transcribe_audio = lambda audio: ('Claire weather', 'en', ())

    frame = np.zeros(int(obj._samplerate * 0.02), dtype=np.float32)
    for _ in range(3):
        obj._feed_streaming_stt(frame)

    assert streaming.accepted == [(len(frame), obj._samplerate)] * 3
    assert streaming.partial_calls == 3
    assert obj._provisional_text() == 'claire'
    assert obj._streaming_segment_started_at is not None

    obj._finalize_streaming_segment()
    assert streaming.finalized == 1
    assert streaming.resets == 1
    assert obj._provisional_text() == ''
    assert obj._streaming_segment_started_at is None


def test_failing_streaming_engine_is_disabled_without_crashing(listener, monkeypatch):
    obj = listener
    streaming = FakeStreamingEngine(fail_on_accept=True)
    inject_engines(monkeypatch, None, streaming)

    obj._feed_streaming_stt(np.zeros(1600, dtype=np.float32))

    assert obj._get_streaming_stt_engine() is None
    assert obj._provisional_text() == ''
    # And the listener still decodes the utterance on the fallback path.
    obj._transcribe_audio = lambda audio: ('Claire hello', 'en', ())
    assert obj._decode_utterance(np.zeros(1600, dtype=np.float32))[0] == 'Claire hello'


def test_provisional_text_can_only_satisfy_wake_detection(listener, monkeypatch):
    obj = listener
    # The streaming recogniser heard the wake word; the authoritative
    # transcript of the same utterance did not. Wake detection may accept the
    # turn, but every word acted on comes from the authoritative transcript.
    obj._process_transcript(
        'what is the weather', captured_during_tts=False,
        captured_tts_start_time=0.0, generation=obj._capture_generation,
        provisional_text='claire what is the weather',
    )
    # The turn was accepted, but every word acted on is authoritative.
    assert obj.state_manager.get_pending_query() == 'what is the weather'
