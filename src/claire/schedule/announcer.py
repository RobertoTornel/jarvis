"""Speak schedule reminders as they come due (R-SCHED-1 announcement loop).

The rules for *when* a reminder is due live in
:mod:`claire.schedule.calendar` (``due_reminders`` / ``describe_reminder`` /
``mark_reminder_fired``) and the file lives in :mod:`claire.schedule.store`.
Both of those were complete and tested while nothing in the running daemon
ever called them: an owner could say "remind me at 6 to call mum", the tool
would book it, and she would never say a word.  A feature that fails silently
is indistinguishable from a feature that does not exist, so this module exists
to be the caller that was missing, and :func:`claire.daemon` starts it on the
shared TTS engine.

The three invariants this loop is built around
-----------------------------------------------
1. **Exactly once, across restarts.**  A reminder is marked fired in the
   schedule file (atomically, via ``store.save_schedule``) only *after* the
   speak call reported success.  Speak fails -> it stays due and the next poll
   tries again; the process dying between speaking and saving can still repeat
   a sentence, which is the cheaper failure compared to losing it forever.
2. **Never talk over a turn.**  If the injected ``is_speaking`` callable says
   speech is already in progress, the whole poll is skipped.  Interrupting the
   owner to read a reminder is worse than reading it one interval later.
3. **A damaged calendar must not kill the loop.**  The owner hand-edits this
   file; a parse error is a Tuesday, not a crash.  Every load/save/lookup step
   is guarded, logged and abandoned for this poll only.

Everything the loop needs is injected — speak, busy-check, path, clock, poll
interval and logger — so tests drive it with no model, socket, display or real
time.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable, List, Optional

from claire.debug import debug_log
from claire.schedule.calendar import describe_reminder, due_reminders, mark_reminder_fired
from claire.schedule.store import load_schedule, resolve_schedule_path, save_schedule

#: How often the daemon polls the schedule for due reminders, in seconds.
#: Module-level on purpose (not a config key) so the startup banner can print
#: the exact value the loop actually uses.
REMINDER_POLL_INTERVAL_SEC = 30

#: How long ``stop()`` waits for the poll thread to finish.
STOP_TIMEOUT_SEC = 5.0


def _default_clock() -> datetime:
    return datetime.now()


def _default_logger(message: str) -> None:
    debug_log(message, "schedule")


class ReminderAnnouncer:
    """Polls the schedule file and speaks the reminders that have come due.

    Args:
        speak: Callable taking the sentence to say.  Returning a falsy value
            (or raising) means "not spoken": the reminder is *not* marked fired
            and the next poll retries it.
        is_speaking: Callable reporting whether speech is already in progress;
            when true the poll is skipped entirely.
        schedule_path: Schedule file to watch; defaults to
            ``store.resolve_schedule_path()`` (``$CLAIRE_SCHEDULE_PATH`` or
            ``~/.local/share/claire/schedule/schedule.json``).
        clock: Zero-argument callable returning "now" (defaults to
            ``datetime.now``), injectable so tests do not sleep on real time.
        poll_interval: Seconds between polls of the loop.
        logger: One-argument callable taking a log line.
    """

    def __init__(
        self,
        speak: Callable[[str], Any],
        is_speaking: Callable[[], bool],
        *,
        schedule_path: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
        poll_interval: float = REMINDER_POLL_INTERVAL_SEC,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not callable(speak):
            raise TypeError("speak must be callable")
        if not callable(is_speaking):
            raise TypeError("is_speaking must be callable")
        self._speak = speak
        self._is_speaking = is_speaking
        self._path = resolve_schedule_path(schedule_path)
        self._clock = clock or _default_clock
        self._poll_interval = float(poll_interval)
        self._logger = logger or _default_logger
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- properties ----------------------------------------------------------
    @property
    def schedule_path(self):
        """The resolved schedule file this announcer reads and writes."""
        return self._path

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- one poll ------------------------------------------------------------
    def poll(self) -> List[str]:
        """Announce every reminder that is due right now.

        Returns the sentences actually spoken, in order — empty when nothing
        was due, when speech was busy, or when the schedule could not be read.
        Each spoken sentence is committed to the schedule file immediately, so
        a later poll (or a restarted daemon) will not repeat it.
        """
        if self._speech_busy():
            return []
        try:
            schedule = load_schedule(self._path)
        except Exception as exc:  # noqa: BLE001 - a damaged file must not kill the loop
            self._log(
                f"reminder poll could not read the schedule "
                f"({type(exc).__name__}: {exc}); skipping this poll"
            )
            return []

        now = self._clock()
        try:
            due = due_reminders(schedule, now)
        except Exception as exc:  # noqa: BLE001 - same for content the rules reject
            self._log(
                f"reminder poll could not evaluate the schedule "
                f"({type(exc).__name__}: {exc}); skipping this poll"
            )
            return []

        spoken: List[str] = []
        for reminder in due:
            try:
                sentence = describe_reminder(reminder, now)
            except Exception as exc:  # noqa: BLE001 - one bad entry must not starve the rest
                self._log(
                    f"could not describe a due reminder "
                    f"({type(exc).__name__}: {exc}); not marking it fired"
                )
                continue
            try:
                said = bool(self._speak(sentence))
            except Exception as exc:  # noqa: BLE001 - a raising engine is a failed speak
                self._log(
                    f"speaking a reminder failed ({type(exc).__name__}: {exc}); "
                    f"not marking it fired, will retry next poll"
                )
                continue
            if not said:
                self._log(
                    f"speech reported failure for {sentence!r}; "
                    f"not marking it fired, will retry next poll"
                )
                continue
            try:
                mark_reminder_fired(schedule, reminder["key"])
                save_schedule(schedule, self._path)
            except Exception as exc:  # noqa: BLE001 - spoken is spoken; report the loss
                self._log(
                    f"reminder was spoken but could not be recorded as fired "
                    f"({type(exc).__name__}: {exc}); it may repeat"
                )
            spoken.append(sentence)
        return spoken

    # -- the loop ------------------------------------------------------------
    def start(self) -> None:
        """Start the daemon thread (polling immediately, then every interval)."""
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="claire-reminder-announcer", daemon=True
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
                self._log(f"reminder poll failed ({type(exc).__name__}: {exc})")
                spoken = []
            if spoken:
                # Say it in the process log as well as out loud.  Her spoken
                # replies already print this way ("🤖 Claire" and then the
                # sentence), and a reminder that only existed at debug level is
                # the same invisibility that hid this feature for a week: the
                # one thing the owner could not tell was whether she had said
                # anything at all.
                for sentence in spoken:
                    print(f"⏰ {sentence}", flush=True)
                self._log("reminders spoken: " + " | ".join(spoken))
            # Event.wait, not sleep: stop() wakes the loop immediately.
            self._stop.wait(self._poll_interval)

    # -- helpers -------------------------------------------------------------
    def _speech_busy(self) -> bool:
        try:
            return bool(self._is_speaking())
        except Exception as exc:  # noqa: BLE001 - a broken engine is not "speaking"
            # Same convention as daemon._phone_speech_state: an engine that
            # cannot answer is not mid-turn, otherwise one exception would
            # stall reminders forever.
            self._log(f"speech busy check failed ({type(exc).__name__}: {exc})")
            return False

    def _log(self, message: str) -> None:
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001 - logging must never break the loop
            pass
