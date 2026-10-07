"""
Regression tests for process-wide serialisation of PortAudio stream
lifecycle calls (the "Fatal Python error: Aborted" crash family, #462,
#401, #422).

PortAudio documents opening and closing streams as NOT thread safe, and its
Windows host APIs abort the whole process on internal assertion failures,
which cannot be caught from Python. Claire touches PortAudio from several
independent threads (the voice listener's run loop, the microphone
permission check, TTS playback, the thinking-tune player), so every stream
lifecycle call must run inside the process-wide ``portaudio_lock``.

That contract is enforced at one surviving chokepoint:
``claire.utils.audio_capture.open_input_stream`` constructs its
``sounddevice.InputStream`` inside ``with portaudio_lock if serialise else
nullcontext():``. Driving that real function is what these tests protect.

The tests install a fake ``sounddevice`` whose ``InputStream`` records
concurrency and the calling thread (widened with a small sleep so overlap
would be visible), then assert:

1. ``serialise=True`` (the default) never lets two threads into the stream
   constructor at once, so no two threads can be inside PortAudio lifecycle
   code simultaneously.
2. ``serialise=False`` is the documented escape hatch for the microphone
   permission probe, whose open can hang indefinitely when Windows blocks
   microphone access — hanging while holding the lock would freeze every
   audio user in the process. Overlapping is allowed there.
3. A rejected capture format (PaError -9998) negotiates another rate or
   channel count instead of failing outright, while a non-format error
   (device busy, access denied) still propagates on the first attempt.
"""

import threading
import time

import pytest

from claire.utils.audio_capture import open_input_stream
from claire.utils.audio_lock import portaudio_lock


class _PaError(Exception):
    """Minimal stand-in for ``sounddevice.PaError``: args are (message, code)."""


class _ConcurrencyProbe:
    """Counts how many threads are inside a stream constructor at once."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.opens = []  # (calling thread ident, samplerate, channels)

    def enter(self, samplerate, channels):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.opens.append((threading.get_ident(), samplerate, channels))
        time.sleep(0.02)  # widen the overlap window

    def exit(self):
        with self._lock:
            self.active -= 1


class _FakeStream:
    def __init__(self, samplerate, channels):
        self.samplerate = samplerate
        self.channels = channels
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSounddevice:
    """The slice of the ``sounddevice`` API ``open_input_stream`` touches.

    ``barrier`` holds every opener until the expected number of them arrive,
    which turns "did two opens overlap?" into a deterministic question.
    ``reject`` decides which formats raise ``error_factory()``.
    """

    def __init__(self, probe, *, barrier=None, reject=None, error_factory=None,
                 default_samplerate=16000, max_input_channels=1):
        self._probe = probe
        self._barrier = barrier
        self._reject = reject
        self._error_factory = error_factory or (lambda: _PaError('Error opening InputStream', -9998))
        self._default_samplerate = default_samplerate
        self._max_input_channels = max_input_channels
        self.attempts = []  # every (samplerate, channels) handed to InputStream

    def InputStream(self, *, samplerate, channels, **kwargs):
        self.attempts.append((samplerate, channels))
        if self._reject is not None and self._reject(samplerate, channels):
            raise self._error_factory()
        self._probe.enter(samplerate, channels)
        try:
            if self._barrier is not None:
                self._barrier.wait(timeout=2)
            return _FakeStream(samplerate, channels)
        finally:
            self._probe.exit()

    def query_devices(self, *args, **kwargs):
        return {
            'index': 0,
            'name': 'Fake microphone',
            'default_samplerate': self._default_samplerate,
            'max_input_channels': self._max_input_channels,
        }


def _assert_lock_was_released():
    """A worker that leaked out still holding the lock would deadlock all audio."""
    assert portaudio_lock.acquire(timeout=1), 'portaudio_lock was left held'
    portaudio_lock.release()


def test_concurrent_opens_are_serialised_by_default():
    """Two threads opening at the same moment must not overlap in PortAudio."""
    probe = _ConcurrencyProbe()
    fake_sd = _FakeSounddevice(probe)
    start = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        try:
            start.wait(timeout=5)  # both threads reach the open together
            stream, rate, channels = open_input_stream(
                fake_sd, 16000, 30, {'device': 3},
            )
            results.append((stream, rate, channels))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f'concurrent opens raised: {errors!r}'
    assert len(results) == 2
    assert probe.max_active == 1, (
        'PortAudio stream lifecycle calls overlapped: '
        f'{probe.max_active} threads were inside InputStream at once'
    )
    # Both opens ran on the thread that asked for them, and both the rate and
    # the channel count are returned to the caller.
    assert {ident for ident, _rate, _ch in probe.opens} == {t.ident for t in threads}
    assert all((rate, channels) == (16000, 1) for _stream, rate, channels in results)
    _assert_lock_was_released()


def test_serialise_false_escape_hatch_allows_overlap():
    """``serialise=False`` is the documented bypass for a hanging probe open."""
    probe = _ConcurrencyProbe()
    # The barrier only clears when both threads are inside the constructor, so
    # this cannot pass if the lock is still being taken.
    fake_sd = _FakeSounddevice(probe, barrier=threading.Barrier(2))
    done = threading.Barrier(2)
    errors = []

    def worker():
        try:
            stream, rate, channels = open_input_stream(
                fake_sd, 16000, 30, {'device': 7}, serialise=False,
                log_category='mic-permission',
            )
            assert (rate, channels) == (16000, 1)
            assert isinstance(stream, _FakeStream)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.wait(timeout=5)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f'unserialised opens raised: {errors!r}'
    assert probe.max_active == 2, (
        'serialise=False did not bypass portaudio_lock: '
        f'peak concurrency was {probe.max_active}'
    )


def test_format_error_negotiates_another_rate_and_channel_count():
    """A PaError -9998 on the first format retries with the fallbacks."""
    probe = _ConcurrencyProbe()
    fake_sd = _FakeSounddevice(
        probe,
        # Reject every mono format, forcing the wider-channel candidates.
        reject=lambda _rate, channels: channels == 1,
        error_factory=lambda: _PaError('Error opening InputStream', -9998),
        default_samplerate=16000,
        max_input_channels=2,
    )

    stream, rate, channels = open_input_stream(
        fake_sd, 16000, 30, {'device': 5}, fallback_rate=44100,
    )

    assert isinstance(stream, _FakeStream)
    # The original mono format is tried first, then the rate fallback, then the
    # two-channel format on the device's native rate.
    assert fake_sd.attempts[0] == (16000, 1)
    assert (44100, 1) in fake_sd.attempts
    assert (rate, channels) == (16000, 2)
    assert fake_sd.attempts[-1] == (16000, 2)
    _assert_lock_was_released()


def test_non_format_error_is_not_retried():
    """Access/device failures must surface immediately, not spin through formats."""
    probe = _ConcurrencyProbe()
    fake_sd = _FakeSounddevice(
        probe,
        reject=lambda _rate, _channels: True,
        error_factory=lambda: RuntimeError('Device unavailable'),
    )

    with pytest.raises(RuntimeError, match='Device unavailable'):
        open_input_stream(fake_sd, 16000, 30, {'device': 5}, fallback_rate=44100)

    assert fake_sd.attempts == [(16000, 1)]
    assert probe.opens == []
    _assert_lock_was_released()
