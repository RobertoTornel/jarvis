"""Native-rate microphone frames reach VAD without silent rejection or loss."""

from collections import deque
import queue
from types import SimpleNamespace

import numpy as np
import pytest

from claire.listening.listener import VoiceListener
import claire.listening.listener as capture
import claire.utils.audio_capture as audio_capture

pytestmark = pytest.mark.unit


def listener(rate=48000):
    obj = VoiceListener.__new__(VoiceListener)
    # These tests drive the pipeline through ``_transcribe_audio``, which is the
    # opt-in Whisper decode entry point: Whisper is not reachable unless
    # ``stt_engine`` names it (Parakeet is the one authoritative recogniser).
    obj.cfg = SimpleNamespace(voice_min_energy=0.0045, voice_debug=False,
                              stt_engine="faster_whisper")
    obj._samplerate = 16000
    obj._stream_samplerate = rate
    obj._frame_samples = rate * 20 // 1000
    obj._pending_audio = None
    obj._recent_audio_energy = deque(maxlen=20)
    obj._vad_error_logged = False
    obj._vad = None
    obj._should_stop = False
    obj._capture_generation = 0
    obj._transcription_worker_thread = None
    import threading
    obj._audio_worker_thread = None
    obj._audio_state_lock = threading.RLock()
    obj._audio_generation = 0
    obj._pre_roll = deque()
    obj._slow_transcription_streak = 0
    obj._slow_transcription_warned = False
    obj._whisper_model_name = "small"
    obj._callback_count = 0
    obj._audio_q = queue.Queue(maxsize=2)
    obj._reset_audio_health(now=0)
    return obj


@pytest.mark.parametrize('rate', [16000, 44100, 48000])
def test_native_frames_use_supported_vad_format(rate):
    obj = listener(rate)

    class StrictVad:
        def is_speech(self, pcm, sample_rate):
            assert sample_rate == 16000
            assert len(pcm) == 640  # 20 ms, mono int16 at 16 kHz
            return True

    obj._vad = StrictVad()
    assert obj._is_speech_frame(np.ones(obj._frame_samples, dtype=np.float32) * .1)


def test_partial_callback_frames_are_not_discarded():
    obj = listener(44100)
    audio = np.linspace(-.1, .1, obj._frame_samples * 3 + 17, dtype=np.float32)
    output = []
    for block in np.array_split(audio, 13):
        output.extend(obj._audio_frames(block[:, None]))
    np.testing.assert_array_equal(np.concatenate(output), audio[:-17])
    np.testing.assert_array_equal(obj._pending_audio, audio[-17:])


def test_vad_failure_warns_once_and_uses_energy_gate(capsys):
    obj = listener()
    class BrokenVad:
        def is_speech(self, *args):
            raise ValueError('invalid frame')
    obj._vad = BrokenVad()
    assert obj._is_speech_frame(np.ones(obj._frame_samples) * .1)
    assert not obj._is_speech_frame(np.zeros(obj._frame_samples))
    assert capsys.readouterr().out.count('Speech detection failed') == 1


def test_capture_health_distinguishes_missing_callbacks_and_silent_samples(capsys):
    obj = listener()
    obj._check_audio_health(now=6)
    assert 'No microphone callbacks' in capsys.readouterr().out
    obj._callback_count = 1
    obj._last_audio_callback = 11
    obj._audio_frames(np.zeros((obj._frame_samples, 1), dtype=np.float32))
    obj._check_audio_health(now=12)
    assert 'silent samples' in capsys.readouterr().out


def test_callback_status_and_queue_overflow_are_visible(capsys):
    obj = listener()
    for _ in range(3):
        obj._on_audio(np.ones((960, 1), dtype=np.float32), 960, None, 'input overflow')
    obj._check_audio_health(now=6)
    output = capsys.readouterr().out
    assert 'input overflow' in output
    assert '1' in output and 'dropped' in output


def test_stalled_capture_warns_once_then_reports_recovery(capsys):
    obj = listener()
    obj._last_audio_callback = 2
    obj._check_audio_health(now=8)
    obj._check_audio_health(now=14)
    assert capsys.readouterr().out.count('No microphone callbacks') == 1
    obj._last_audio_callback = 19
    obj._audio_frames(np.ones((obj._frame_samples, 1), dtype=np.float32) * .1)
    obj._check_audio_health(now=20)
    assert 'arriving again' in capsys.readouterr().out


def test_callback_exception_is_not_silenced(capsys):
    obj = listener()
    class BrokenInput:
        def copy(self):
            raise RuntimeError('capture buffer failed')
    obj._on_audio(BrokenInput(), 960, None, None)
    obj._check_audio_health(now=6)
    assert 'capture buffer failed' in capsys.readouterr().out


@pytest.mark.parametrize('rate,channels', [(16000, 2), (48000, 2), (44100, 4), (48000, 1)])
def test_capture_negotiates_format_on_selected_device(monkeypatch, rate, channels):
    opened = []
    stream = object()

    def open_stream(**kwargs):
        assert kwargs['device'] == 7
        opened.append((kwargs['samplerate'], kwargs['channels']))
        if kwargs['channels'] != channels:
            raise RuntimeError('Invalid number of channels', -9998)
        if kwargs['samplerate'] != rate:
            raise RuntimeError('Invalid sample rate', -9997)
        assert kwargs['blocksize'] == rate * 20 // 1000
        return stream

    monkeypatch.setattr(capture.sd, 'InputStream', open_stream)
    def device_info(device=None, **kwargs):
        assert device == 7
        return {'max_input_channels': channels, 'default_samplerate': rate}
    monkeypatch.setattr(capture.sd, 'query_devices', device_info)
    result, actual_rate, actual_channels = audio_capture.open_input_stream(
        capture.sd, 16000, 20, {'device': 7}, callback=lambda *args: None,
    )
    assert result is stream
    assert (actual_rate, actual_channels) == (rate, channels)
    assert len(opened) == len(set(opened)) <= 6


@pytest.mark.parametrize('failure', ['Microphone access denied', 'Device unavailable', 'Device busy'])
def test_capture_does_not_retry_non_format_errors(monkeypatch, failure):
    def open_stream(**kwargs):
        raise RuntimeError(failure)
    def unexpected_query(*args, **kwargs):
        pytest.fail('must not negotiate a permission or device availability failure')
    monkeypatch.setattr(capture.sd, 'InputStream', open_stream)
    monkeypatch.setattr(capture.sd, 'query_devices', unexpected_query)
    with pytest.raises(RuntimeError, match=failure):
        audio_capture.open_input_stream(capture.sd, 16000, 20, {})


def test_multichannel_capture_preserves_signal_outside_first_channel():
    obj = listener(48000)
    source = np.zeros((obj._frame_samples * 2 + 13, 2), dtype=np.float32)
    source[:, 1] = np.linspace(-.5, .5, len(source))
    frames = []
    for block in np.array_split(source, 7):
        frames.extend(obj._audio_frames(block))
    output = np.concatenate(frames + [obj._pending_audio])
    np.testing.assert_allclose(output, source.mean(axis=1))
    assert np.max(np.abs(output)) > 0


def test_unsupported_device_exhausts_bounded_formats(monkeypatch):
    attempts = []
    def reject(**kwargs):
        attempts.append((kwargs['samplerate'], kwargs['channels']))
        raise RuntimeError('Invalid number of channels', -9998)
    monkeypatch.setattr(capture.sd, 'InputStream', reject)
    def default_input(**kwargs):
        assert kwargs == {'kind': 'input'}
        return {'max_input_channels': 4, 'default_samplerate': 48000}
    monkeypatch.setattr(capture.sd, 'query_devices', default_input)
    with pytest.raises(RuntimeError, match='Invalid number of channels'):
        audio_capture.open_input_stream(capture.sd, 16000, 20, {})
    assert len(attempts) == len(set(attempts)) == 6


def test_access_failure_during_negotiation_stops_retries(monkeypatch):
    attempts = []
    def reject(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError('Invalid number of channels', -9998)
        if len(attempts) > 2:
            pytest.fail('must stop after an access failure')
        raise RuntimeError('Access denied')
    monkeypatch.setattr(capture.sd, 'InputStream', reject)
    monkeypatch.setattr(capture.sd, 'query_devices', lambda **kwargs: {
        'max_input_channels': 2, 'default_samplerate': 48000,
    })
    with pytest.raises(RuntimeError, match='Access denied'):
        audio_capture.open_input_stream(capture.sd, 16000, 20, {})


@pytest.mark.parametrize('serialise', [False, True])
def test_stream_open_lock_policy_includes_retries(monkeypatch, serialise):
    locked = []
    class Lock:
        def __enter__(self):
            locked.append(True)
        def __exit__(self, *args):
            locked.pop()
    monkeypatch.setattr(audio_capture, 'portaudio_lock', Lock())
    def open_stream(**kwargs):
        assert bool(locked) == serialise
        if kwargs['channels'] == 1:
            raise RuntimeError('Invalid number of channels', -9998)
        return 'stream'
    monkeypatch.setattr(capture.sd, 'InputStream', open_stream)
    monkeypatch.setattr(capture.sd, 'query_devices', lambda **kwargs: {
        'max_input_channels': 2, 'default_samplerate': 16000,
    })
    assert audio_capture.open_input_stream(capture.sd, 16000, 20, {}, serialise=serialise)[0] == 'stream'
    assert not locked


def test_missing_named_input_does_not_record_another_microphone(monkeypatch, capsys):
    obj = listener()
    obj.cfg.voice_device = 'Disconnected Headset'
    monkeypatch.setattr(capture.sd, 'query_devices', lambda: [
        {'name': 'Built-in Microphone', 'max_input_channels': 1},
    ])
    monkeypatch.setattr(capture.sd, 'InputStream', lambda **kwargs: pytest.fail('unexpected capture'))
    obj.run()
    assert 'Selected microphone not found' in capsys.readouterr().out


def test_capture_reports_configured_format_error_after_all_retries(monkeypatch):
    def reject(**kwargs):
        raise RuntimeError(f"Unsupported {kwargs['samplerate']} Hz / {kwargs['channels']} channels", -9998)

    monkeypatch.setattr(capture.sd, 'InputStream', reject)
    monkeypatch.setattr(capture.sd, 'query_devices', lambda **kwargs: {
        'max_input_channels': 2, 'default_samplerate': 48000,
    })

    with pytest.raises(RuntimeError, match='Unsupported 16000 Hz / 1 channels'):
        audio_capture.open_input_stream(capture.sd, 16000, 20, {})


def test_capture_keeps_original_error_when_device_query_fails(monkeypatch):
    original = RuntimeError('Invalid number of channels', -9998)
    monkeypatch.setattr(capture.sd, 'InputStream', lambda **kwargs: (_ for _ in ()).throw(original))
    monkeypatch.setattr(capture.sd, 'query_devices', lambda **kwargs: (_ for _ in ()).throw(OSError('device disconnected')))

    with pytest.raises(RuntimeError) as raised:
        audio_capture.open_input_stream(capture.sd, 16000, 20, {})
    assert raised.value is original


def test_named_input_skips_devices_with_missing_names(monkeypatch):
    monkeypatch.setattr(capture.sd, 'query_devices', lambda: [
        {'name': None, 'max_input_channels': 1},
        {'name': 'Headset microphone', 'max_input_channels': 1},
    ])
    assert audio_capture.resolve_input_device(capture.sd, 'Headset') == {'device': 1}


def test_numeric_input_index_zero_is_selected():
    assert audio_capture.resolve_input_device(capture.sd, 0) == {'device': 0}


def test_utterance_finalisation_queues_transcription_without_blocking():
    from types import SimpleNamespace

    obj = listener()
    obj._utterance_frames = [np.ones(8000, dtype=np.float32)]
    obj._samplerate = obj._stream_samplerate = 16000
    obj._transcription_jobs_q = queue.Queue(maxsize=8)
    obj._transcription_results_q = queue.Queue()
    obj.is_speech_active = True
    obj._silence_frames = 0
    obj.echo_detector = SimpleNamespace(_utterance_start_time=1.0, _tts_start_time=0,
                                        _last_tts_finish_time=0, echo_tolerance=0.3)
    obj.tts = None
    obj._capture_generation = 0
    obj.cfg = SimpleNamespace(voice_debug=False, whisper_min_audio_duration=0.3)
    obj._calculate_audio_energy = lambda frames: 0.1

    obj._finalize_utterance()

    assert not obj.is_speech_active
    assert obj._utterance_frames == []
    assert obj._transcription_jobs_q.qsize() == 1
    assert obj._transcription_results_q.empty()
    job = obj._transcription_jobs_q.get_nowait()
    assert job.audio.shape == (8000,)
    assert job.start_time == 1.0
    assert job.energy == 0.1


def test_transcription_worker_preserves_utterance_order():
    import threading

    obj = listener()
    obj._transcription_jobs_q = queue.Queue()
    obj._transcription_results_q = queue.Queue()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    started = []

    def transcribe(audio):
        started.append(audio)
        if audio == "first":
            first_started.set()
            release_first.wait(timeout=2)
        else:
            second_started.set()
        return str(audio), "en", ()

    obj._transcribe_audio = transcribe
    obj._transcription_jobs_q.put(SimpleNamespace(audio="first", start_time=1.0, end_time=2.0, energy=0.1, capture_generation=0, captured_during_tts=False, captured_tts_start_time=0))
    obj._transcription_jobs_q.put(SimpleNamespace(audio="second", start_time=2.0, end_time=3.0, energy=0.2, capture_generation=0, captured_during_tts=False, captured_tts_start_time=0))
    obj._transcription_jobs_q.put(None)

    worker = threading.Thread(target=obj._run_transcription_worker)
    worker.start()
    try:
        assert first_started.wait(timeout=1)
        assert not second_started.wait(timeout=0.05)
    finally:
        release_first.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert started == ["first", "second"]
    results = [obj._transcription_results_q.get_nowait() for _ in range(2)]
    assert [result.text for result in results] == ["first", "second"]


def test_utterance_finalisation_continues_while_whisper_is_busy():
    import threading
    from types import SimpleNamespace

    obj = listener()
    obj._transcription_jobs_q = queue.Queue(maxsize=8)
    obj._transcription_results_q = queue.Queue()
    whisper_started = threading.Event()
    release_whisper = threading.Event()

    def transcribe(audio):
        if isinstance(audio, str):
            whisper_started.set()
            release_whisper.wait(timeout=2)
        return "recognised", "en", ()

    obj._transcribe_audio = transcribe
    obj._transcription_jobs_q.put(
        SimpleNamespace(audio="slow", start_time=1.0, end_time=2.0, energy=0.1, capture_generation=0, captured_during_tts=False, captured_tts_start_time=0)
    )
    worker = threading.Thread(target=obj._run_transcription_worker)
    worker.start()

    try:
        assert whisper_started.wait(timeout=1)
        obj._utterance_frames = [np.ones(8000, dtype=np.float32)]
        obj._samplerate = obj._stream_samplerate = 16000
        obj.is_speech_active = True
        obj._silence_frames = 0
        obj.echo_detector = SimpleNamespace(_utterance_start_time=1.0, _tts_start_time=0,
                                            _last_tts_finish_time=0, echo_tolerance=0.3)
        obj.cfg = SimpleNamespace(voice_debug=False, whisper_min_audio_duration=0.3,
                                  stt_engine="faster_whisper")
        obj.tts = None
        obj._calculate_audio_energy = lambda frames: 0.1

        obj._finalize_utterance()

        assert obj._transcription_jobs_q.qsize() == 1
        assert worker.is_alive()
    finally:
        release_whisper.set()
        obj._transcription_jobs_q.put(None)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert [obj._transcription_results_q.get_nowait().text for _ in range(2)] == [
        "recognised",
        "recognised",
    ]


def test_transcription_result_keeps_language_and_utterance_context():
    from types import SimpleNamespace
    from unittest.mock import Mock

    obj = listener()
    obj._last_detected_language = None
    obj._first_utterance = True
    obj.tts = None
    obj.echo_detector = SimpleNamespace(_tts_start_time=0, _last_tts_finish_time=0, echo_tolerance=0.3)
    obj._transcript_buffer = Mock()
    obj._process_transcript = Mock()
    obj._is_repetitive_hallucination = lambda text: False
    result = SimpleNamespace(
        text="hello there",
        language="en",
        low_confidence_events=(),
        start_time=10.0,
        end_time=11.0,
        energy=0.2,
        capture_generation=0,
        captured_during_tts=False,
        captured_tts_start_time=0,
    )

    obj._handle_transcription_result(result)

    assert obj._last_detected_language == "en"
    obj._transcript_buffer.add.assert_called_once_with(
        text="hello there",
        start_time=10.0,
        end_time=11.0,
        energy=0.2,
        is_during_tts=False,
    )
    obj._process_transcript.assert_called_once_with(
        "hello there", 0.2, 10.0, 11.0,
        generation=result.capture_generation,
        captured_during_tts=False, captured_tts_start_time=0,
    )


def test_shutdown_discards_full_backlog_without_waiting_for_whisper():
    import threading
    from unittest.mock import Mock

    obj = listener()
    obj._transcription_jobs_q = queue.Queue(maxsize=1)
    obj._transcription_results_q = queue.Queue()
    obj._handle_transcription_result = Mock()
    started = threading.Event()
    release = threading.Event()

    def transcribe(_audio):
        started.set()
        release.wait(timeout=3)
        return "late answer", "en", ()

    obj._transcribe_audio = transcribe
    job = SimpleNamespace(audio="active", start_time=1.0, end_time=2.0, energy=0.1, capture_generation=0, captured_during_tts=False, captured_tts_start_time=0)
    obj._transcription_jobs_q.put(job)
    obj._start_transcription_worker()
    assert started.wait(timeout=1)
    obj._transcription_jobs_q.put(SimpleNamespace(audio="queued", start_time=2.0, end_time=3.0, energy=0.1, capture_generation=0, captured_during_tts=False, captured_tts_start_time=0))
    obj._should_stop = True
    finished = threading.Event()

    def finish():
        obj._finish_transcription_worker()
        finished.set()

    shutdown = threading.Thread(target=finish, daemon=True)
    shutdown.start()
    try:
        assert finished.wait(timeout=1), "shutdown blocked on queued or active Whisper work"
        assert obj._transcription_jobs_q.qsize() <= 1
        obj._handle_transcription_result.assert_not_called()
    finally:
        release.set()
        shutdown.join(timeout=3)


def test_stale_transcription_result_is_not_dispatched():
    from unittest.mock import Mock

    obj = listener()
    obj._capture_generation = 1
    obj._last_detected_language = "fr"
    obj._first_utterance = True
    obj.tts = None
    obj.echo_detector = SimpleNamespace(_tts_start_time=0, _last_tts_finish_time=0, echo_tolerance=0.3)
    obj._transcript_buffer = Mock()
    obj._process_transcript = Mock()
    obj._is_repetitive_hallucination = lambda text: False
    result = SimpleNamespace(text="claire do this", language="en", low_confidence_events=(), start_time=10.0, end_time=11.0, energy=0.2, capture_generation=0, captured_during_tts=False, captured_tts_start_time=0)

    obj._handle_transcription_result(result)

    assert obj._last_detected_language == "fr"
    obj._transcript_buffer.add.assert_not_called()
    obj._process_transcript.assert_not_called()


def test_transcription_result_without_detected_language_clears_previous_locale():
    from unittest.mock import Mock

    obj = listener()
    obj._last_detected_language = "fr"
    obj._first_utterance = True
    obj.tts = None
    obj.echo_detector = SimpleNamespace(_tts_start_time=0, _last_tts_finish_time=0, echo_tolerance=0.3)
    obj._transcript_buffer = Mock()
    obj._process_transcript = Mock()
    obj._is_repetitive_hallucination = lambda text: False

    obj._handle_transcription_result(SimpleNamespace(text="hello", language=None, low_confidence_events=(), start_time=10.0, end_time=11.0, energy=0.2, capture_generation=0, captured_during_tts=False, captured_tts_start_time=0))

    assert obj._last_detected_language is None


def test_echo_flag_uses_capture_interval_when_tts_starts_during_whisper():
    from unittest.mock import Mock

    obj = listener()
    obj._last_detected_language = None
    obj._first_utterance = True
    obj.tts = SimpleNamespace(is_speaking=lambda: True)
    obj.echo_detector = SimpleNamespace(_tts_start_time=12.0, _last_tts_finish_time=0, echo_tolerance=0.3)
    obj._transcript_buffer = Mock()
    obj._process_transcript = Mock()
    obj._is_repetitive_hallucination = lambda text: False

    obj._handle_transcription_result(SimpleNamespace(text="claire hello", language="en", low_confidence_events=(), start_time=10.0, end_time=11.0, energy=0.2, capture_generation=0, captured_during_tts=False, captured_tts_start_time=0))

    assert obj._transcript_buffer.add.call_args.kwargs["is_during_tts"] is False


def test_queued_job_keeps_tts_context_from_audio_capture():
    from unittest.mock import Mock

    obj = listener()
    obj._utterance_frames = [np.ones(8000, dtype=np.float32)]
    obj._samplerate = obj._stream_samplerate = 16000
    obj._transcription_jobs_q = queue.Queue(maxsize=8)
    obj._transcription_results_q = queue.Queue()
    obj.is_speech_active = True
    obj._silence_frames = 0
    obj.echo_detector = SimpleNamespace(_utterance_start_time=10.0, _tts_start_time=0,
                                        _last_tts_finish_time=0, echo_tolerance=0.3)
    obj.tts = SimpleNamespace(is_speaking=Mock(return_value=False))
    obj.cfg = SimpleNamespace(voice_debug=False, whisper_min_audio_duration=0.3)
    obj._calculate_audio_energy = lambda frames: 0.1

    obj._finalize_utterance()
    job = obj._transcription_jobs_q.get_nowait()
    obj.echo_detector._tts_start_time = 12.0
    obj.tts.is_speaking.return_value = True
    obj._transcribe_audio = lambda audio: ("stop", "en", ())
    obj._transcription_jobs_q.put(job)
    obj._transcription_jobs_q.put(None)
    obj._run_transcription_worker()
    result = obj._transcription_results_q.get_nowait()

    assert result.captured_during_tts is False
    assert result.captured_tts_start_time == 0


def test_run_cleans_up_worker_after_listener_exception():
    import threading
    from unittest.mock import Mock

    obj = listener()
    obj._transcription_jobs_q = queue.Queue(maxsize=1)
    obj._transcription_results_q = queue.Queue()
    obj._handle_transcription_result = Mock()
    obj._transcribe_audio = lambda audio: ("hello", "en", ())
    obj._start_transcription_worker()
    worker = obj._transcription_worker_thread
    obj._run = lambda: (_ for _ in ()).throw(RuntimeError("capture failed"))

    with pytest.raises(RuntimeError, match="capture failed"):
        obj.run()

    worker.join(timeout=1)
    assert not worker.is_alive()
    assert obj._should_stop


def test_capture_generation_change_invalidates_decode_even_after_resume():
    import threading
    from unittest.mock import Mock

    obj = listener()
    obj._transcription_jobs_q = queue.Queue(maxsize=1)
    obj._transcription_results_q = queue.Queue()
    started = threading.Event()
    release = threading.Event()
    obj._handle_transcription_result = Mock()

    def transcribe(_audio):
        started.set()
        release.wait(timeout=2)
        return "claire do this", "en", ()

    obj._transcribe_audio = transcribe
    obj._start_transcription_worker()
    worker = obj._transcription_worker_thread
    obj._transcription_jobs_q.put(SimpleNamespace(audio="before pause", start_time=1.0, end_time=2.0, energy=0.1, capture_generation=0, captured_during_tts=False, captured_tts_start_time=0))
    assert started.wait(timeout=1)
    obj._capture_generation += 1
    release.set()
    obj._transcription_jobs_q.put(None)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert obj._transcription_results_q.empty()


@pytest.mark.parametrize(
    "captured_during_tts,captured_tts_start_time,should_interrupt",
    [(False, 0.0, False), (True, 12.0, True), (True, 9.0, False)],
)
def test_delayed_stop_only_interrupts_tts_that_overlapped_capture(
    captured_during_tts, captured_tts_start_time, should_interrupt,
):
    from unittest.mock import Mock

    obj = listener()
    obj.cfg.wake_word = "claire"
    obj.cfg.wake_aliases = []
    obj.cfg.tune_enabled = False
    obj.tts = SimpleNamespace(enabled=True, is_speaking=lambda: True, interrupt=Mock())
    obj.echo_detector = SimpleNamespace(_tts_start_time=12.0, _last_tts_finish_time=0,
                                        _last_tts_text="", echo_tolerance=0.3)
    obj.state_manager = Mock()
    obj.state_manager.was_speech_during_hot_window.return_value = False
    obj.state_manager.is_collecting.return_value = False
    obj._intent_judge = None
    obj._transcript_buffer = Mock()
    obj._stop_thinking_tune = Mock()
    obj._is_thinking_tune_active = lambda: False
    obj._start_thinking_tune = Mock()
    obj._set_face_state_listening = Mock()
    obj._clear_audio_buffers = Mock()
    obj._wake_timestamp = None

    start_time, end_time = ((12.1, 12.5) if captured_tts_start_time == 12.0 and captured_during_tts
                            else (10.0, 11.0))
    obj._process_transcript("stop", 0.2, start_time, end_time,
                            captured_during_tts=captured_during_tts,
                            captured_tts_start_time=captured_tts_start_time, generation=obj._capture_generation)

    assert obj.tts.interrupt.called is should_interrupt


def test_delayed_utterance_does_not_inherit_later_tts_text_for_intent():
    from unittest.mock import Mock

    obj = listener()
    obj.cfg.wake_word = "claire"
    obj.cfg.wake_aliases = []
    obj.cfg.tune_enabled = False
    obj.tts = SimpleNamespace(enabled=True, is_speaking=lambda: False)
    obj.echo_detector = SimpleNamespace(_tts_start_time=12.0, _last_tts_finish_time=13.0,
                                        _last_tts_text="future reply", echo_tolerance=0.3)
    obj.state_manager = Mock()
    obj.state_manager.was_speech_during_hot_window.return_value = False
    obj.state_manager.is_collecting.return_value = False
    obj._intent_judge = Mock(available=True)
    obj._intent_judge.judge.return_value = None
    obj._intent_judge.last_failure_reason = "unavailable"
    obj._transcript_buffer = Mock()
    obj._buffer_duration = 120
    obj._stop_thinking_tune = Mock()
    obj._is_thinking_tune_active = lambda: False
    obj._start_thinking_tune = Mock()
    obj._set_face_state_listening = Mock()
    obj._clear_audio_buffers = Mock()
    obj._wake_timestamp = None

    obj._process_transcript("claire weather", 0.2, 10.0, 11.0,
                            captured_during_tts=False, captured_tts_start_time=0, generation=obj._capture_generation)

    assert obj._intent_judge.judge.call_args.kwargs["last_tts_text"] == ""


@pytest.mark.parametrize('configured', [None, '', 'default', 'system'])
def test_default_input_is_pinned_across_capture_phases(configured):
    default = {'index': 3, 'max_input_channels': 1}
    opened = []

    def query_devices(*, kind):
        assert kind == 'input'
        return default.copy()

    def open_stream(**kwargs):
        opened.append(kwargs['device'])
        return object()

    audio = SimpleNamespace(query_devices=query_devices, InputStream=open_stream)
    selected = audio_capture.resolve_input_device(audio, configured)
    audio_capture.open_input_stream(audio, 16000, 20, selected)
    default['index'] = 7
    audio_capture.open_input_stream(audio, 16000, 20, selected)
    assert opened == [3, 3]


def test_missing_default_input_is_actionable_without_selecting_another_device():
    def missing_default(*, kind):
        raise RuntimeError('Error querying device -1')

    audio = SimpleNamespace(query_devices=missing_default)
    with pytest.raises(ValueError, match='default microphone.*Settings'):
        audio_capture.resolve_input_device(audio, None, [
            {'index': 2, 'name': 'Other microphone', 'max_input_channels': 1},
        ])


def test_output_only_default_is_not_accepted_as_a_microphone():
    audio = SimpleNamespace(query_devices=lambda **kwargs: {
        'index': 2, 'max_input_channels': 0,
    })
    with pytest.raises(ValueError, match='default microphone.*Settings'):
        audio_capture.resolve_input_device(audio, None)


def test_listener_reports_missing_default_before_loading_whisper(monkeypatch, capsys):
    obj = listener()
    obj.cfg.voice_device = None
    obj._warm_up_models = lambda: pytest.fail('unexpected model warm-up')

    def devices(*args, **kwargs):
        if kwargs.get('kind') == 'input':
            raise RuntimeError('Error querying device -1')
        return [{'index': 0, 'name': 'Available mic', 'max_input_channels': 1}]

    monkeypatch.setattr(capture.sd, 'query_devices', devices)
    monkeypatch.setattr(capture.sd, 'InputStream', lambda **kwargs: pytest.fail('unexpected capture'))
    monkeypatch.setattr(capture, 'WhisperModel', lambda *args, **kwargs: pytest.fail('unexpected model load'))
    obj.run()
    assert 'default microphone' in capsys.readouterr().out

@pytest.fixture(autouse=True)
def cached_whisper_files(monkeypatch):
    """Model-loading tests use synthetic local files and never access the Hub."""
    monkeypatch.setattr("claire.listening.model_download.prepare_faster_whisper_model",
                        lambda name: name)


@pytest.mark.parametrize('frame_ms,tts_speaking,limit_frames,silent_endpoint', [
    (10, False, 7, False), (20, False, 11, False),
    (30, False, 5, False), (20, True, 6, False),
    (20, False, 1, False), (20, False, 11, True),
])
def test_speech_reaches_whisper_at_configured_boundary(
    monkeypatch, frame_ms, tts_speaking, limit_frames, silent_endpoint,
):
    from unittest.mock import MagicMock
    from claire.config import load_settings

    from dataclasses import replace
    cfg = replace(
        load_settings(), voice_debug=False, vad_enabled=False,
        vad_frame_ms=frame_ms, vad_pre_roll_ms=0,
        max_utterance_ms=limit_frames * frame_ms,
        tts_max_utterance_ms=(limit_frames if tts_speaking else 2) * frame_ms,
        whisper_backend='faster-whisper', whisper_device='cpu',
        whisper_min_audio_duration=0, endpoint_silence_ms=3 * frame_ms,
        # This test is about where capture boundaries fall, so the speech gate
        # (covered by its own tests below) is switched off to isolate it.
        min_speech_ms=0, noise_gate_enabled=False,
    )
    monkeypatch.setattr(capture, 'create_intent_judge', lambda _cfg: None)
    monkeypatch.setattr(capture, 'FASTER_WHISPER_AVAILABLE', True)
    monkeypatch.setattr(capture, '_load_faster_whisper_model', lambda *a, **kw: MagicMock())
    monkeypatch.setattr(VoiceListener, '_decode_faster_whisper', lambda *a: ([], None))
    monkeypatch.setattr(VoiceListener, '_start_llm_warmup', lambda _self: [])
    monkeypatch.setattr(VoiceListener, '_start_transcription_worker', lambda _self: None)
    monkeypatch.setattr('desktop_app.face_widget.get_claire_state', lambda: MagicMock())
    obj = VoiceListener(MagicMock(), cfg, SimpleNamespace(is_speaking=lambda: tts_speaking), None)
    frame_samples = cfg.sample_rate * frame_ms // 1000
    speech_frames = limit_frames - 2 if silent_endpoint else limit_frames * 3
    speech = np.linspace(.05, .2, frame_samples * speech_frames, dtype=np.float32)
    audio = (np.concatenate([speech, np.zeros(frame_samples * 3, dtype=np.float32)])
             if silent_endpoint else speech)

    class Feed(queue.Queue):
        def get(self, *args, **kwargs):
            if self.empty():
                obj._should_stop = True
                raise queue.Empty
            return super().get(*args, **kwargs)

    obj._audio_q = Feed()

    class Stream:
        active = True
        def start(self):
            obj._on_audio(audio[:, None], len(audio), None, None)
        def stop(self):
            pass
        def close(self):
            pass

    def devices(device=None, **kwargs):
        info = {'index': 0, 'name': 'Synthetic input', 'max_input_channels': 1}
        return info if device is not None or kwargs else [info]
    monkeypatch.setattr(capture, 'sd', SimpleNamespace(
        query_devices=devices, InputStream=lambda **kw: Stream(),
    ))
    obj.run()

    jobs = list(obj._transcription_jobs_q.queue)
    expected_count = 1 if silent_endpoint else 3
    expected_frames = speech_frames if silent_endpoint else limit_frames
    assert len(jobs) == expected_count, 'Speech must be submitted at its capture boundary'
    assert all(len(job.audio) == frame_samples * expected_frames for job in jobs)
    np.testing.assert_array_equal(np.concatenate([job.audio for job in jobs]), speech)


# --- Speech gate: ambient noise must not become an utterance -------------------
#
# Buffered duration alone is not evidence of speech. Pre-roll pads a one-frame
# noise click out to roughly a quarter of a second, which cleared the old
# minimum-duration check and let silence decode into stray words such as
# "Yeah." and "Mhm.". These tests pin the replacement: a gate that looks at how
# much audio VAD actually called speech, relative to the room's own noise floor.


def speech_gate_listener(min_speech_ms=250.0, noise_gate_enabled=True,
                         noise_gate_margin=2.0, noise_gate_min_rms=0.002,
                         ambient=None):
    obj = listener(16000)
    obj.cfg = SimpleNamespace(min_speech_ms=min_speech_ms,
                              noise_gate_enabled=noise_gate_enabled,
                              noise_gate_margin=noise_gate_margin,
                              noise_gate_min_rms=noise_gate_min_rms)
    obj._utterance_voice_frames = 0
    obj._utterance_speech_rms_sum = 0.0
    obj._ambient_energy = deque(ambient or [], maxlen=1500)
    return obj


def _voice(obj, frames, rms):
    """Record ``frames`` VAD-positive 20 ms frames at level ``rms``."""
    obj._utterance_voice_frames = frames
    obj._utterance_speech_rms_sum = frames * rms
    return obj


def test_accepted_utterances_report_the_evidence_that_cleared_the_gate():
    """The gate is measurable in both directions.

    Discards logged why (``discarded utterance: ...``); accepts logged nothing,
    so a noisy room that produced stray decodes left no record of how close to
    the thresholds they were. This is the line that makes the next such report
    tunable.
    """
    obj = _voice(speech_gate_listener(ambient=[0.001] * 200), frames=25, rms=0.01)
    obj._utterance_frames = [b"\x00" * 64] * 25

    evidence = obj._speech_evidence()

    assert "buffered 500 ms" in evidence
    assert "vad speech 500 ms" in evidence
    assert "audible floor 0.00200" in evidence
    assert "room floor 0.00100" in evidence
    assert "x2.0 = 0.00200" in evidence
    assert "speech rms 0.01000" in evidence

def test_the_evidence_report_survives_an_utterance_with_no_frames():
    """A report, not a ZeroDivisionError, when there is nothing to report."""
    obj = _voice(speech_gate_listener(), frames=0, rms=0.0)

    evidence = obj._speech_evidence()

    assert "vad speech 0 ms" in evidence
    assert "speech rms 0.00000" in evidence

# A near-silent room is the case the relative test cannot see: its own floor is
# ~0, so `floor * margin` is smaller than the converter's dither and every
# noise blip clears it. Measured on a real microphone (47% input volume), the
# room floor was 0.00008 and dither sat at 0.0002-0.0015 - the whole range that
# cleared the gate and came back from the recogniser as "Yeah.", "Okay." and
# "Uh.". Real speech on the same microphone measured 0.00226-0.00926.

def test_dither_is_rejected_when_the_room_floor_is_near_zero():
    obj = _voice(speech_gate_listener(ambient=[0.00008] * 200),
                 frames=78, rms=0.00038)  # the 1.56 s "Yeah."

    reason = obj._speech_gate_reason()

    assert reason is not None and 'audible floor' in reason

def test_real_speech_clears_the_audible_floor():
    # "Claire, do you hear me?" - the quietest real utterance measured.
    obj = _voice(speech_gate_listener(ambient=[0.00008] * 200),
                 frames=75, rms=0.00226)

    assert obj._speech_gate_reason() is None

def test_the_audible_floor_is_the_highest_dither_that_used_to_pass():
    # 0.0015 was the loudest hallucination observed: one more hair and the
    # shipped floor would stop catching it.
    obj = _voice(speech_gate_listener(ambient=[0.00008] * 200),
                 frames=25, rms=0.00151)

    reason = obj._speech_gate_reason()

    assert reason is not None and 'audible floor' in reason

def test_the_audible_floor_can_be_disabled():
    # An escape hatch for a microphone quieter than the shipped floor: 0.0
    # leaves only the relative test.
    obj = _voice(speech_gate_listener(noise_gate_min_rms=0.0,
                                      ambient=[0.00008] * 200),
                 frames=25, rms=0.0003)

    assert obj._speech_gate_reason() is None

def test_turning_the_gate_off_disables_both_level_tests():
    obj = _voice(speech_gate_listener(noise_gate_enabled=False,
                                      ambient=[0.00008] * 200),
                 frames=25, rms=0.00002)

    assert obj._speech_gate_reason() is None

# The floor is read off whatever config object the listener was handed. Test
# doubles are `MagicMock`s, and `float(MagicMock())` is 1.0 - full scale - so a
# floor taken at face value there does not make the gate stricter, it makes it
# deaf and the assistant transcribes nothing at all. (That was a real
# regression: it silenced this gate for every mock-config test.)

def test_a_floor_no_voice_could_clear_falls_back_to_the_default():
    from unittest.mock import MagicMock

    obj = speech_gate_listener()
    obj.cfg = MagicMock()  # every attribute reads as a Mock, and floatifies to 1.0

    assert obj._audible_floor() == pytest.approx(0.002)

@pytest.mark.parametrize("value,expected", [
    (0.0, 0.0),            # deliberately disabled
    (0.004, 0.004),        # a real level
    ("0.003", 0.003),      # hand-edited config.json
    (-0.5, 0.0),           # config.py clamps negatives to "disabled" too
    (float("nan"), 0.002),  # not a level
    (float("inf"), 0.002),
    (1.0, 0.002),          # full scale: no voice clears it
    (0.75, 0.002),         # nor this
    (None, 0.002),
    ("loud", 0.002),
])
def test_the_floor_is_used_only_when_it_could_be_a_voice_level(value, expected):
    obj = speech_gate_listener()
    obj.cfg.noise_gate_min_rms = value

    assert obj._audible_floor() == pytest.approx(expected)

def test_a_config_without_the_key_still_gets_the_default_floor():
    # A duck-typed config (or an older Settings) must not become a deaf one.
    obj = _voice(speech_gate_listener(), frames=25, rms=0.1)
    obj.cfg = SimpleNamespace(min_speech_ms=250.0, noise_gate_enabled=True,
                              noise_gate_margin=2.0)

    assert obj._audible_floor() == pytest.approx(0.002)
    assert obj._speech_gate_reason() is None

def test_padded_noise_click_fails_the_speech_gate():
    # One 20 ms blip is 20 ms of speech no matter how much pre-roll precedes it.
    obj = _voice(speech_gate_listener(), frames=1, rms=0.05)

    reason = obj._speech_gate_reason()

    assert reason is not None and 'too short' in reason


def test_real_syllable_clears_the_speech_gate():
    obj = _voice(speech_gate_listener(), frames=25, rms=0.05)  # 500 ms at 20 ms

    assert obj._speech_gate_reason() is None


def test_utterance_without_vad_positive_audio_is_rejected():
    obj = _voice(speech_gate_listener(), frames=0, rms=0.0)

    assert obj._speech_gate_reason() == 'no VAD-positive audio'


def test_disabling_the_speech_gate_allows_a_single_frame():
    obj = _voice(speech_gate_listener(min_speech_ms=0), frames=1, rms=0.05)

    assert obj._speech_gate_reason() is None


def test_speech_below_the_room_noise_floor_is_rejected():
    # A loud room: the floor sits at 1.0e-2, so 1.5e-2 is audible (it clears the
    # absolute floor) but still does not stand above the room it is in.
    obj = _voice(speech_gate_listener(ambient=[0.01] * 200), frames=25, rms=0.015)

    reason = obj._speech_gate_reason()

    assert reason is not None and 'noise floor' in reason


def test_speech_above_the_room_noise_floor_passes():
    obj = _voice(speech_gate_listener(ambient=[0.001] * 200), frames=25, rms=0.01)

    assert obj._speech_gate_reason() is None


def test_noise_floor_gate_is_inert_until_the_ambient_history_fills():
    # Start-up: too little history to know the room, so the *relative* level
    # test must not reject. (The absolute floor still applies - it does not
    # depend on knowing anything about the room - so the level here is audible.)
    obj = _voice(speech_gate_listener(ambient=[0.5] * 5), frames=25, rms=0.01)

    assert obj._speech_gate_reason() is None


def test_noise_floor_gate_can_be_disabled():
    obj = _voice(speech_gate_listener(noise_gate_enabled=False,
                                      ambient=[0.001] * 200), frames=25, rms=0.0015)

    assert obj._speech_gate_reason() is None


def test_discarding_a_noisy_utterance_clears_its_accounting():
    obj = _voice(speech_gate_listener(), frames=1, rms=0.05)
    obj.is_speech_active = True
    obj._silence_frames = 7
    obj._utterance_frames = [np.ones(320, dtype=np.float32)]

    obj._discard_noisy_utterance('too short')

    assert obj.is_speech_active is False
    assert obj._silence_frames == 0
    assert obj._utterance_frames == []
    assert obj._utterance_voice_frames == 0
    assert obj._utterance_speech_rms_sum == 0.0


def _click_with_pre_roll(monkeypatch, min_speech_ms):
    """Run one quiet frame run, one loud click, then endpoint silence.

    The buffered utterance is pre-roll plus the click, which is long enough to
    clear the older duration check. Returns the transcription jobs queued.
    """
    from dataclasses import replace
    from unittest.mock import MagicMock
    from claire.config import load_settings

    frame_ms = 20
    cfg = replace(
        load_settings(), voice_debug=False, vad_enabled=False,
        vad_frame_ms=frame_ms, vad_pre_roll_ms=240,
        endpoint_silence_ms=3 * frame_ms, max_utterance_ms=12000,
        whisper_backend='faster-whisper', whisper_device='cpu',
        whisper_min_audio_duration=0.15,
        min_speech_ms=min_speech_ms, noise_gate_enabled=False,
    )
    monkeypatch.setattr(capture, 'create_intent_judge', lambda _cfg: None)
    monkeypatch.setattr(capture, 'FASTER_WHISPER_AVAILABLE', True)
    monkeypatch.setattr(capture, '_load_faster_whisper_model', lambda *a, **kw: MagicMock())
    monkeypatch.setattr(VoiceListener, '_decode_faster_whisper', lambda *a: ([], None))
    monkeypatch.setattr(VoiceListener, '_start_llm_warmup', lambda _self: [])
    monkeypatch.setattr(VoiceListener, '_start_transcription_worker', lambda _self: None)
    monkeypatch.setattr('desktop_app.face_widget.get_claire_state', lambda: MagicMock())
    obj = VoiceListener(MagicMock(), cfg, SimpleNamespace(is_speaking=lambda: False), None)

    frame_samples = cfg.sample_rate * frame_ms // 1000
    quiet = np.full(frame_samples * 20, 0.001, dtype=np.float32)
    click = np.full(frame_samples, 0.2, dtype=np.float32)
    audio = np.concatenate([quiet, click, np.full(frame_samples * 4, 0.001, dtype=np.float32)])

    class Feed(queue.Queue):
        def get(self, *args, **kwargs):
            if self.empty():
                obj._should_stop = True
                raise queue.Empty
            return super().get(*args, **kwargs)

    obj._audio_q = Feed()

    class Stream:
        active = True
        def start(self):
            obj._on_audio(audio[:, None], len(audio), None, None)
        def stop(self):
            pass
        def close(self):
            pass

    def devices(device=None, **kwargs):
        info = {'index': 0, 'name': 'Synthetic input', 'max_input_channels': 1}
        return info if device is not None or kwargs else [info]

    monkeypatch.setattr(capture, 'sd', SimpleNamespace(
        query_devices=devices, InputStream=lambda **kw: Stream(),
    ))
    obj.run()
    return list(obj._transcription_jobs_q.queue)


def test_noise_click_padded_by_pre_roll_is_not_transcribed(monkeypatch):
    jobs = _click_with_pre_roll(monkeypatch, min_speech_ms=250.0)

    assert jobs == [], 'A one-frame noise click must never become a transcription'


def test_same_click_would_be_transcribed_without_the_speech_gate(monkeypatch):
    # Guards the test above from passing vacuously: the audio does reach the
    # queue when the gate is off, so it is the gate that rejects it.
    jobs = _click_with_pre_roll(monkeypatch, min_speech_ms=0)

    assert len(jobs) == 1

