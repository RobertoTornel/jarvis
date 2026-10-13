"""Speak countdown timers the moment they are up (R-SCHED-1 timer loop).

:mod:`claire.schedule.timers` holds the whole countdown model —
``TimerManager.start``/``cancel``/``list_active``/``fire_due``/``remaining`` —
and had done so, complete and tested, while nothing in the running product ever
imported it: "Claire, set a timer for ten minutes" had never worked, because
there was no caller.  This module is the caller that fires timers, and
:func:`claire.daemon` starts it on the shared TTS engine.  A timer that reaches
zero in silence is indistinguishable from a timer that was never set.

The four invariants this loop is built around
---------------------------------------------
1. **Exactly once.**  ``fire_due()`` *removes* a due timer inside the same lock
   that selects it, so a timer is handed out once and can never be announced
   twice, even by two pollers racing.  That is also why a busy speech check must
   come *first*: consuming a due timer while the owner is mid-sentence would
   delete it unannounced.
2. **A failed speak must not lose the sentence.**  Because ``fire_due()`` has
   already consumed the timer, a speak that returns false or raises would drop
   it forever if the sentence were simply discarded.  Those timers are held in a
   pending list and retried on the next poll (retries first, oldest first, then
   newly due ones) until speech accepts them.  This list is memory-only, exactly
   like the timers themselves: a timer that was due but not yet spoken when
   Claire restarts is gone, and the ``PERSISTENCE_NOTE`` the tool reads back to
   the owner already says so.
3. **Never talk over a turn.**  If the injected ``is_speaking`` callable reports
   that speech is in progress, the whole poll returns without consuming
   anything — pending and due timers alike stay where they are.
4. **A manager that raises must not kill the loop.**  Every call into the timer
   manager is guarded, logged and abandoned for this poll only.

The poll is deliberately much tighter than the reminder loop's 30 seconds: a
countdown timer is expected to fire *now* (the owner is watching a pasta pot,
not a calendar), so a half-second interval is the difference between "the timer
went off" and "the timer went off a bit late".  A reminder read thirty seconds
late is still a reminder; a timer read thirty seconds late is a burnt dinner.
That is why the interval lives here as a module constant, not in config.

Everything the loop needs is injected — speak, busy-check, manager, clock, poll
interval and logger — so tests drive it with no model, socket, display or real
time.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, List, Optional

from claire.debug import debug_log
from claire.schedule.timers import Timer, TimerManager, default_manager

#: How often the daemon polls the timer manager, in seconds.  Half a second, so
#: a due timer is spoken within about a second of its deadline; the reminder
#: loop's 30 s would make a countdown useless.  Module-level on purpose (not a
#: config key) so the startup banner can print the exact value the loop uses.
TIMER_POLL_INTERVAL_SEC = 0.5

#: How long ``stop()`` waits for the poll thread to finish.
STOP_TIMEOUT_SEC = 5.0


def _default_logger(message: str) -> None:
    debug_log(message, "schedule")


class TimerAnnouncer:
    """Polls a :class:`TimerManager` and speaks the timers that have elapsed.

    Args:
        speak: Callable taking the sentence to say.  Returning a falsy value (or
            raising) means "not spoken": the timer stays pending and the next
            poll retries it.
        is_speaking: Callable reporting whether speech is already in progress;
            when true the poll consumes nothing at all.
        manager: The live timers to watch; defaults to the process-wide
            :func:`claire.schedule.timers.default_manager` — the same instance
            the ``timer`` tool writes to, so a timer the owner sets is one this
            loop will actually fire.
        clock: The line of numbers the manager's deadlines were measured with.
            ``None`` (the default) means "use the manager's own clock", which is
            the right answer for the daemon; pass one only in tests that injected
            the same clock into the manager.
        poll_interval: Seconds between polls of the loop.
        logger: One-argument callable taking a log line.
    """

    def __init__(
        self,
        speak: Callable[[str], Any],
        is_speaking: Callable[[], bool],
        *,
        manager: Optional[TimerManager] = None,
        clock: Optional[Callable[[], float]] = None,
        poll_interval: float = TIMER_POLL_INTERVAL_SEC,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not callable(speak):
            raise TypeError("speak must be callable")
        if not callable(is_speaking):
            raise TypeError("is_speaking must be callable")
        self._speak = speak
        self._is_speaking = is_speaking
        self._manager = manager if manager is not None else default_manager()
        self._clock = clock
        self._poll_interval = float(poll_interval)
        self._logger = logger or _default_logger
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Timers that were due but whose sentence speech has not accepted yet.
        self._pending: List[Timer] = []

    # -- properties ----------------------------------------------------------
    @property
    def manager(self) -> TimerManager:
        """The manager this announcer watches (shared with the ``timer`` tool)."""
        return self._manager

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def pending_count(self) -> int:
        """How many already-fired sentences are still waiting to be spoken."""
        return len(self._pending)

    # -- one poll ------------------------------------------------------------
    def poll(self) -> List[str]:
        """Speak every timer that is up right now.

        Returns the sentences actually spoken, in order — empty when nothing was
        due, when speech was busy, or when the manager could not be read.  A
        timer whose sentence speech refused is kept pending and retried, never
        dropped: ``fire_due`` already removed it from the manager.
        """
        if self._speech_busy():
            # Consume nothing while the owner is being spoken to; in particular
            # do not call fire_due, because that would delete a due timer that
            # nobody has heard yet.
            return []

        spoken: List[str] = []

        # Retries first, so a sentence that has been waiting is not starved by a
        # steady stream of newly due ones.  The list is already oldest-first:
        # fire_due returns timers sorted by (deadline, number) and appends in
        # that order.
        pending, self._pending = self._pending, []
        if pending:
            self._log(
                f"retrying {len(pending)} timer sentence(s) speech did not accept"
            )
        for timer in pending:
            sentence = self._format(timer)
            if sentence is None:
                continue
            if self._say(sentence):
                spoken.append(sentence)
            else:
                self._pending.append(timer)

        try:
            due = self._due()
        except Exception as exc:  # noqa: BLE001 - a broken manager must not kill the loop
            self._log(
                f"timer poll could not read the timers "
                f"({type(exc).__name__}: {exc}); skipping this poll"
            )
            return spoken

        for timer in due:
            sentence = self._format(timer)
            if sentence is None:
                continue
            if self._say(sentence):
                spoken.append(sentence)
            else:
                self._pending.append(timer)
        return spoken

    # -- the loop ------------------------------------------------------------
    def start(self) -> None:
        """Start the daemon thread (polling immediately, then every interval)."""
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="claire-timer-announcer", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_SEC) -> None:
        """Signal the loop and join it promptly (never waits out an interval)."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                spoken = self.poll()
            except Exception as exc:  # noqa: BLE001 - last-resort guard for the loop
                self._log(f"timer poll failed ({type(exc).__name__}: {exc})")
                spoken = []
            if spoken:
                # Say it in the process log as well as out loud.  Her spoken
                # replies print this way, and a timer that only existed at debug
                # level is the same invisibility that hid the reminder loop: the
                # one thing the owner could not tell was whether she had said
                # anything at all.
                for sentence in spoken:
                    print(f"⏳ {sentence}", flush=True)
                self._log("timers spoken: " + " | ".join(spoken))
            # Event.wait, not sleep: stop() wakes the loop immediately.
            self._stop.wait(self._poll_interval)

    # -- helpers -------------------------------------------------------------
    def _due(self) -> List[Timer]:
        """The timers that have elapsed, removed from the manager as they go."""
        if self._clock is None:
            return self._manager.fire_due()
        return self._manager.fire_due(now=self._clock())

    def _format(self, timer: Timer) -> Optional[str]:
        """The sentence for one elapsed timer, or ``None`` if it cannot be built."""
        try:
            return timer.format_finished()
        except Exception as exc:  # noqa: BLE001 - one odd timer must not starve the rest
            self._log(
                f"could not describe an elapsed timer "
                f"({type(exc).__name__}: {exc}); dropping it"
            )
            return None

    def _say(self, sentence: str) -> bool:
        """Speak one sentence; false (and a log line) when it was not said."""
        try:
            said = bool(self._speak(sentence))
        except Exception as exc:  # noqa: BLE001 - a raising engine is a failed speak
            self._log(
                f"speaking a timer failed ({type(exc).__name__}: {exc}); "
                f"keeping it pending, will retry next poll"
            )
            return False
        if not said:
            self._log(
                f"speech reported failure for {sentence!r}; "
                f"keeping it pending, will retry next poll"
            )
            return False
        return True

    def _speech_busy(self) -> bool:
        try:
            return bool(self._is_speaking())
        except Exception as exc:  # noqa: BLE001 - a broken engine is not "speaking"
            # Same convention as daemon._phone_speech_state: an engine that
            # cannot answer is not mid-turn, otherwise one exception would
            # stall timers forever.
            self._log(f"speech busy check failed ({type(exc).__name__}: {exc})")
            return False

    def _log(self, message: str) -> None:
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001 - logging must never break the loop
            pass
