"""Playback for short pre-recorded speech clips.

This is the sibling of :mod:`claire.output.tune_player`: the same
``sounddevice``/PortAudio path, the same one-thread-owns-the-stream lifecycle,
and the same rule that a playback thread closes the stream it is driving.
Keeping to one audio API means the clip releases the device in milliseconds,
so live speech synthesis can open it immediately afterwards.

Two details are load-bearing and are why this module exists rather than a
direct ``sounddevice.play`` call:

* every PortAudio stream lifecycle call runs inside ``portaudio_lock``, and the
  lock is never held across a playback wait (see
  :mod:`claire.utils.audio_lock`);
* the stopping thread only sets an event. The owning thread closes the stream,
  because ``abort`` from one thread while another closes the same stream races
  on device teardown and crashes the process instead of raising.

A clip that cannot be played returns ``False`` rather than raising: the caller
has live synthesis to fall back on, and a missing or busy device must not take
a turn down with it.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

import numpy as np

from ..debug import debug_log
from ..utils.audio_lock import portaudio_lock

#: Category used for every debug line this module emits.
CLIP_DEBUG_CATEGORY = "clips"
#: Frames per audio callback. Small enough that a stop is heard promptly.
CLIP_BLOCKSIZE = 1024
#: How long playback may outlast the clip's own length before it is closed.
#: Covers the device's own buffering of the final block.
CLIP_TAIL_GRACE_SEC = 0.25
#: How often the owning thread checks whether the clip has finished.
CLIP_POLL_SEC = 0.02


class ClipPlayer:
    """Plays one clip at a time, returning immediately and stopping cleanly."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._thread: Optional[threading.Thread] = None
        self._busy = False
        self._stop_event = threading.Event()
        self._is_playing = threading.Event()
        self._lock = threading.Lock()

    def is_speaking(self) -> bool:
        """Whether a clip is being played right now.

        Matches the contract of ``tts.is_speaking()`` so the listener can avoid
        talking over itself.
        """
        return self._is_playing.is_set()

    def play(self, clip: Any, on_finished: Optional[Callable[[], None]] = None) -> bool:
        """Start playing ``clip`` and return at once.

        Returns True when the stream is running, False when there is nothing to
        play, a clip is already playing, or the output device could not be
        opened. ``on_finished`` runs on the owning thread only when the clip
        reached its end; it does not run when playback was stopped early.
        """
        if not self.enabled or clip is None:
            return False
        samples = np.asarray(getattr(clip, "samples", None))
        rate = int(getattr(clip, "sample_rate", 0) or 0)
        if samples.size == 0 or rate <= 0:
            debug_log("clips: refusing an empty clip", CLIP_DEBUG_CATEGORY)
            return False

        with self._lock:
            if self._busy:
                debug_log("clips: a clip is already playing", CLIP_DEBUG_CATEGORY)
                return False
            self._busy = True

        def release() -> None:
            with self._lock:
                self._busy = False

        try:
            import sounddevice as sd
        except Exception as exc:
            debug_log(
                f"clips: sounddevice unavailable, cannot play a clip: {exc!r}",
                CLIP_DEBUG_CATEGORY,
            )
            release()
            return False

        total = int(samples.size)
        position = [0]

        def callback(outdata: Any, frames: int, time_info: Any, status: Any) -> None:
            start = position[0]
            end = start + frames
            if end <= total:
                outdata[:, 0] = samples[start:end]
                position[0] = end
                return
            remaining = total - start
            if remaining > 0:
                outdata[:remaining, 0] = samples[start:total]
            outdata[max(remaining, 0):, 0] = 0
            position[0] = total

        stream = None
        try:
            with portaudio_lock:
                stream = sd.OutputStream(
                    samplerate=rate,
                    channels=1,
                    dtype="int16",
                    blocksize=CLIP_BLOCKSIZE,
                    latency="low",
                    callback=callback,
                )
                stream.start()
        except Exception as exc:
            debug_log(
                f"clips: could not open the output device: {exc!r}", CLIP_DEBUG_CATEGORY
            )
            if stream is not None:
                try:
                    with portaudio_lock:
                        stream.close()
                except Exception:
                    pass
            release()
            return False

        self._stop_event.clear()
        self._is_playing.set()
        thread = threading.Thread(
            target=self._finish,
            args=(stream, total, rate, on_finished, position),
            daemon=True,
            name="clip-playback",
        )
        with self._lock:
            self._thread = thread
        thread.start()
        debug_log(
            f"clips: playing {getattr(clip, 'phrase', '?')!r} "
            f"({total / float(rate):.2f}s)",
            CLIP_DEBUG_CATEGORY,
        )
        return True

    def stop(self, wait: bool = True, timeout: float = 2.0) -> None:
        """Stop playback and release the audio device.

        The stream is closed by its owning thread, never here: ``abort`` from
        the stopping thread races with that close on device teardown.
        """
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._is_playing.clear()

    # -- internals ---------------------------------------------------------

    def _finish(
        self,
        stream: Any,
        total: int,
        rate: int,
        on_finished: Optional[Callable[[], None]],
        position: list,
    ) -> None:
        deadline = time.monotonic() + (total / float(rate)) + CLIP_TAIL_GRACE_SEC
        finished = False
        try:
            while not self._stop_event.is_set():
                if position[0] >= total:
                    finished = True
                    break
                if time.monotonic() >= deadline:
                    debug_log(
                        "clips: clip outlasted its own length; closing the stream",
                        CLIP_DEBUG_CATEGORY,
                    )
                    break
                self._stop_event.wait(CLIP_POLL_SEC)
            if finished and on_finished is not None:
                try:
                    on_finished()
                except Exception as exc:
                    debug_log(
                        f"clips: completion callback failed: {exc!r}", CLIP_DEBUG_CATEGORY
                    )
        finally:
            try:
                with portaudio_lock:
                    stream.close()
            except Exception as exc:
                debug_log(f"clips: closing the stream failed: {exc!r}", CLIP_DEBUG_CATEGORY)
            self._is_playing.clear()
            with self._lock:
                self._thread = None
                self._busy = False
