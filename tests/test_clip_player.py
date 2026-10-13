"""Behavioural tests for the pre-recorded speech-clip player.

Covers the contract the listener relies on:

- ``play`` starts a clip and returns promptly;
- ``is_speaking`` reports playback so the listener can avoid talking over
  itself;
- ``stop`` releases the device from the playing thread only, never by calling
  ``abort`` from the stopping thread (the two race on device teardown);
- playback degrades to "did not play" rather than raising, so the caller can
  fall back to live synthesis.

The sounddevice stream is a fake injected into ``sys.modules``; no audio
hardware is touched.
"""
from __future__ import annotations

import sys
import threading
import time
import types

import numpy as np
import pytest

from claire.output.clip_cache import Clip
from claire.output.clip_player import ClipPlayer

pytestmark = pytest.mark.unit


def clip(seconds=0.2, rate=22050, value=1500):
    samples = np.full(int(rate * seconds), value, dtype=np.int16)
    return Clip(phrase="On it.", samples=samples, sample_rate=rate)


class _FakeStream:
    """Minimal sounddevice.OutputStream stand-in."""

    def __init__(self, *args, **kwargs):
        self.started = False
        self.aborted = False
        self.closed = False
        self._callback = kwargs.get("callback")

    def start(self):
        self.started = True
        if self._callback is not None:
            threading.Thread(target=self._drive_callbacks, daemon=True).start()

    def _drive_callbacks(self, frames=1024):
        """Hand the callback buffers until the stream is closed.

        The real audio thread does this; a test needs it to happen too, or a
        clip could never reach its end.
        """
        while not self.closed:
            try:
                self._callback(np.zeros((frames, 1), dtype=np.int16), frames, None, None)
            except Exception:
                return
            time.sleep(0.001)

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True


def _install_fake_sounddevice(monkeypatch, stream_factory=None, fail_open=False):
    created = {}

    def _OutputStream(*args, **kwargs):
        if fail_open:
            raise RuntimeError("no default output device")
        stream = (stream_factory or _FakeStream)(*args, **kwargs)
        created["stream"] = stream
        return stream

    fake_sd = types.ModuleType("sounddevice")
    fake_sd.OutputStream = _OutputStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    return created


def _wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_disabled_player_never_plays(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=False)

    assert player.play(clip()) is False
    assert not player.is_speaking()


def test_play_starts_the_stream_and_returns_promptly(monkeypatch):
    created = _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=True)

    started = time.time()
    assert player.play(clip(seconds=0.05)) is True
    elapsed = time.time() - started

    assert elapsed < 0.5, "play must not block for the clip's duration"
    assert created["stream"].started
    player.stop()


def test_is_speaking_is_true_during_playback_and_false_after_stop(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=True)

    player.play(clip(seconds=1.0))
    assert player.is_speaking() is True

    player.stop()
    assert player.is_speaking() is False


def test_stop_closes_the_stream_from_the_playing_thread(monkeypatch):
    created = _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=True)
    player.play(clip(seconds=1.0))
    assert _wait_until(lambda: "stream" in created and created["stream"].started)

    started = time.time()
    player.stop()
    elapsed = time.time() - started

    stream = created["stream"]
    assert stream.closed, "the owning thread must close the stream"
    assert not stream.aborted, "abort from the stopping thread races with close"
    assert elapsed < 1.0, "stop must release the device quickly"


def test_play_finishes_on_its_own_for_a_short_clip(monkeypatch):
    created = _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=True)

    player.play(clip(seconds=0.02))
    assert _wait_until(lambda: not player.is_speaking(), timeout=2.0)

    assert created["stream"].closed


def test_stop_without_play_is_harmless(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=True)

    player.stop()
    player.stop()

    assert not player.is_speaking()


def test_play_while_playing_is_refused(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=True)

    assert player.play(clip(seconds=0.5)) is True
    assert player.play(clip(seconds=0.5)) is False
    player.stop()


def test_play_of_none_is_refused(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=True)

    assert player.play(None) is False
    assert not player.is_speaking()


def test_play_returns_false_when_sounddevice_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    player = ClipPlayer(enabled=True)

    assert player.play(clip()) is False
    assert not player.is_speaking()


def test_play_returns_false_when_the_device_cannot_open(monkeypatch):
    _install_fake_sounddevice(monkeypatch, fail_open=True)
    player = ClipPlayer(enabled=True)

    assert player.play(clip()) is False
    assert not player.is_speaking()


def test_on_finished_runs_when_the_clip_ends(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=True)
    finished = threading.Event()

    player.play(clip(seconds=0.02), on_finished=finished.set)

    assert finished.wait(2.0), "the completion callback must fire"
    assert not player.is_speaking()


def test_on_finished_does_not_run_when_stopped_early(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = ClipPlayer(enabled=True)
    finished = threading.Event()

    player.play(clip(seconds=5.0), on_finished=finished.set)
    player.stop()

    assert not finished.is_set()
