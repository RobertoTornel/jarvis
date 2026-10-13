"""Continuation phrases for a turn that runs long.

One "On it." does not cover a wait that can run twenty seconds. A
:class:`ContinuationSchedule` plays a further short phrase at fixed marks after
dispatch, so the person knows Claire is still working, and cancels instantly
when the answer arrives or the turn is abandoned.

Phrases are chosen least-recently-used by the listener (see
``listener._pick_continuation_phrase``), mirroring the acknowledgement's
existing rotation rather than adding a second mechanism.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Collection, Optional, Sequence

from ..debug import debug_log

#: Seconds after dispatch at which a further phrase is spoken.
DEFAULT_CONTINUATION_MARKS_SEC = (8.0, 18.0, 30.0)


def pick_least_recently_used(
    order: Sequence[str], exclude: Collection[str] = ()
) -> Optional[str]:
    """The phrase to speak next, given least-recently-used ``order``.

    ``exclude`` holds phrases already spoken this turn; they are skipped while
    any unspoken phrase remains. When every phrase is excluded the least
    recently used one is repeated rather than saying nothing.
    """
    phrases = [phrase for phrase in order if phrase]
    if not phrases:
        return None
    already_spoken = set(exclude or ())
    for phrase in phrases:
        if phrase not in already_spoken:
            return phrase
    return phrases[0]


class ContinuationSchedule:
    """Calls ``play`` at each mark, on a daemon thread, until cancelled.

    ``play`` is invoked on the schedule's own thread and must return promptly;
    ``is_active`` is consulted before every mark so a turn that has ended, or a
    reply already speaking, is never talked over.
    """

    def __init__(
        self,
        marks: Sequence[float],
        play: Callable[[], None],
        *,
        is_active: Optional[Callable[[], bool]] = None,
        category: str = "voice",
    ) -> None:
        self._marks = tuple(float(mark) for mark in marks)
        self._play = play
        self._is_active = is_active
        self._category = category
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Begin the schedule; a no-op when there are no marks or it is running."""
        with self._lock:
            if self._thread is not None or not self._marks:
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run, daemon=True, name="ack-continuations"
            )
            self._thread = thread
            thread.start()

    def cancel(self, wait: bool = True, timeout: float = 1.0) -> None:
        """Stop the schedule now; no further phrase will be played."""
        self._stop.set()
        with self._lock:
            thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def is_running(self) -> bool:
        """Whether the schedule thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- internals ---------------------------------------------------------

    def _run(self) -> None:
        started = time.monotonic()
        try:
            for mark in self._marks:
                remaining = mark - (time.monotonic() - started)
                if remaining > 0 and self._stop.wait(remaining):
                    return
                if self._stop.is_set():
                    return
                if self._is_active is not None and not self._is_active():
                    debug_log("continuations: the turn is not active, stopping", self._category)
                    return
                try:
                    self._play()
                except Exception as exc:
                    debug_log(
                        f"continuations: phrase failed: {exc!r}", self._category
                    )
        finally:
            with self._lock:
                self._thread = None
