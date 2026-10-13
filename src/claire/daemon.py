"""
Claire Voice Assistant Daemon

Main orchestrator that coordinates listening, reply generation, and output.
"""

from __future__ import annotations
import sys
import os
import time
import signal
import threading
import contextlib
from collections import deque
from dataclasses import replace

# Fix OpenBLAS threading crash in bundled apps (must be before numpy imports)
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('MKL_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')

# Fix Windows console encoding for Unicode/emoji characters
# Skip in bundled mode (frozen) - encoding is handled by desktop_app.py
if sys.platform == 'win32' and not getattr(sys, 'frozen', False):
    try:
        import io
        # Only wrap the real console streams. Test harnesses (pytest) and
        # embedding code replace sys.stdout/sys.stderr with their own capture
        # objects; wrapping those detaches and closes their buffers, which
        # corrupts output capture for the rest of the process.
        if (sys.stdout is sys.__stdout__ and hasattr(sys.stdout, 'buffer')
                and hasattr(sys.stdout.buffer, 'write')):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        if (sys.stderr is sys.__stderr__ and hasattr(sys.stderr, 'buffer')
                and hasattr(sys.stderr.buffer, 'write')):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

from typing import Any, Optional

from .config import Settings, load_settings
from .memory.db import Database
from .memory.conversation import DialogueMemory, update_diary_from_dialogue_memory
from .output.tts import SUPPORTED_TTS_ENGINES, create_tts_engine
from .tools.registry import initialize_mcp_tools
from .debug import debug_log
from .schedule.announcer import REMINDER_POLL_INTERVAL_SEC, ReminderAnnouncer
from .schedule.timer_announcer import TIMER_POLL_INTERVAL_SEC, TimerAnnouncer
from .control.server import ControlSocketServer
from .schedule.timers import default_manager
from .listening.listener import VoiceListener
from .listening.wake_detection import is_wake_word_detected
from .utils.location import get_location_context


class _UnavailableTTS:
    """Stand-in for a TTS engine that could not be initialised.

    Speech output is unavailable, so this never speaks and never claims to be
    speaking. It exists so the face-widget poller (``get_tts_engine()``) and
    the voice listener can keep asking the same questions they always ask
    without a ``None`` crash — while ``enabled`` stays honestly ``False``.
    """

    enabled = False
    unavailable_reason = "speech output unavailable: no TTS engine was initialised"

    def is_speaking(self) -> bool:
        """No engine means nothing can be audible."""
        return False

    def speak(self, text: Any = None, completion_callback: Any = None,
              **kwargs: Any) -> bool:
        """Accept the call and report that nothing was spoken."""
        debug_log(f"{self.unavailable_reason}; dropping reply: {text!r}", "tts")
        return False

    def interrupt(self) -> None:
        """Nothing to interrupt."""
        return None

    def start(self) -> None:
        """Nothing to start."""
        return None

    def stop(self) -> None:
        """Nothing to stop."""
        return None


def _initialize_tts_engine(cfg) -> Optional[Any]:
    """Build the configured TTS engine, degrading honestly when it cannot.

    Returns the engine, or ``None`` when it could not be built — an
    unrecognised ``tts_engine`` name, a missing voice model, a missing Fish
    key, and so on. The reason is printed, naming the configured value and the
    engines that do exist: an unknown name is never quietly substituted,
    because speaking in a voice the user did not ask for is worse than not
    speaking at all.
    """
    engine_name = getattr(cfg, "tts_engine", None)
    print(f"🔊 Initializing TTS engine ({engine_name})...", flush=True)
    try:
        tts = create_tts_engine(
            engine=engine_name,
            enabled=cfg.tts_enabled,
            voice=cfg.tts_voice,
            # ``config`` supplies every Fish default (voice id, key file, latency,
            # model). Without it each `_fish_option` fallback reads from None, so
            # the engine is built with no voice id and `start()` then refuses -
            # correctly, per N10, and Claire ends up silent. This one argument is
            # the difference between Fish working and Fish never working.
            config=cfg,
            rate=cfg.tts_rate,
            # Chatterbox parameters
            device=cfg.tts_chatterbox_device,
            audio_prompt_path=cfg.tts_chatterbox_audio_prompt,
            exaggeration=cfg.tts_chatterbox_exaggeration,
            cfg_weight=cfg.tts_chatterbox_cfg_weight,
            # Piper parameters
            piper_model_path=cfg.tts_piper_model_path,
            piper_speaker=cfg.tts_piper_speaker,
            piper_length_scale=cfg.tts_piper_length_scale,
            piper_noise_scale=cfg.tts_piper_noise_scale,
            piper_noise_w=cfg.tts_piper_noise_w,
            piper_sentence_silence=cfg.tts_piper_sentence_silence,
        )
    except Exception as exc:
        # Not swallowed: the configured name, the failure and the supported
        # engines are all reported, and Claire keeps running without speech.
        supported = ", ".join(repr(name) for name in SUPPORTED_TTS_ENGINES)
        print(f"❌ TTS engine {engine_name!r} could not be initialised: {exc}", flush=True)
        print(f"   Supported engines are: {supported}", flush=True)
        print("  ⚠️ Speech output unavailable. Claire will continue without spoken replies.", flush=True)
        debug_log(f"TTS engine init failed for {engine_name!r}: {exc}", "tts")
        return None
    return tts


def _start_tts_engine(tts) -> bool:
    """Start speech output and report whether it is actually available.

    Returns ``True`` only when speech can really be produced after this call.
    ``tts.start()`` is allowed to raise: the Fish engine builds its client -
    and so reads its key file - lazily, and refuses a missing, empty or
    world-readable key file, or an absent voice id, rather than report speech
    it cannot make (rule N10). That refusal is converted here into *disabled
    speech*, never into a dead daemon: the reason is printed and logged, and
    the caller replaces the engine with an inert stand-in.
    """
    if tts is None or isinstance(tts, _UnavailableTTS):
        print("  ⚠️ Speech output unavailable (no TTS engine was initialised).", flush=True)
        return False
    if not tts.enabled:
        print("  🔇 TTS disabled", flush=True)
        return False
    try:
        tts.start()
    except Exception as exc:
        # Refused speech must mean *disabled speech*, never a dead daemon. The
        # Fish engine builds its client - and so reads its key file - lazily and
        # refuses a missing, empty or world-readable key file, or an absent
        # voice id, rather than report speech it cannot make (rule N10). The
        # daemon owns the decision to keep running, so the daemon owns the catch.
        print(
            f"  ⚠️ TTS engine failed to start; speech output unavailable: {exc}",
            flush=True,
        )
        debug_log(f"TTS engine start refused ({type(exc).__name__}): {exc}", "tts")
        return False
    if tts.enabled:
        print("✅ TTS engine started", flush=True)
        return True
    print("  ⚠️ Speech output unavailable. Check the voice model settings and restart Claire.", flush=True)
    return False


# Global instances for coordination between modules
_global_dialogue_memory: Optional[DialogueMemory] = None
_global_stop_requested: bool = False
_warm_profile_graph_listener = None  # registered callback, kept for shutdown unregister
_global_tts_engine = None  # TTS engine reference for face animation polling
# Config + DB booted by main(). Shared by the voice listener and the text-chat
# submission path so voice and text are one conversation against one store.
_global_cfg = None
_global_db = None

# Shutdown timeout for diary update (shorter than normal to allow reasonable quit time)
# Desktop app's stop_daemon() should wait at least this long + buffer
SHUTDOWN_DIARY_TIMEOUT_SEC = 45.0

# Callbacks for desktop app to receive diary update progress
# Set by desktop app before calling request_stop()
_diary_update_callbacks: dict = {
    "on_token": None,  # Callable[[str], None] - called for each LLM token
    "on_status": None,  # Callable[[str], None] - called for status updates
    "on_chunks": None,  # Callable[[List[str]], None] - called with pending chunks
    "on_complete": None,  # Callable[[bool], None] - called when done (success/fail)
}

# One query at a time: voice and text share this lock so they cannot race the
# dialogue memory. Held for the duration of a single reply-engine run.
_chat_query_lock = threading.Lock()

# Per-query cancellation flag for the text-chat path. Set by
# ``cancel_active_chat_query`` (the chat window's Stop button), checked by the
# chat worker after ``run_reply_engine`` returns so the reply is dropped
# instead of displayed. This is distinct from ``request_stop`` (daemon
# lifecycle shutdown) — cancelling a chat query must not tear down the voice
# assistant.
_chat_cancel_event: Optional[threading.Event] = None

# Chat IPC protocol prefixes - desktop app intercepts lines starting with these.
# __CHAT__:        daemon -> desktop (event stream, mirrors DIARY_IPC_PREFIX)
# __CHAT_QUERY__:  desktop -> daemon (query submission, read from stdin)
CHAT_IPC_PREFIX = "__CHAT__:"
CHAT_QUERY_IPC_PREFIX = "__CHAT_QUERY__:"
# Cancellation travels the same way a submission does. In subprocess
# mode the query runs here, in the daemon, whose module globals are a
# different instance from the desktop app's: calling the cancel
# function over there sets a flag nobody in this process reads.
CHAT_CANCEL_IPC_PREFIX = "__CHAT_CANCEL__"
# Session control (subprocess mode): new session (bare line), rewind to a
# user turn, and restore an archived session. All operate on the daemon's
# shared dialogue memory, which is where the conversation actually lives.
CHAT_NEW_SESSION_IPC_PREFIX = "__CHAT_NEW_SESSION__"
CHAT_REWIND_IPC_PREFIX = "__CHAT_REWIND__:"
CHAT_RESTORE_IPC_PREFIX = "__CHAT_RESTORE__:"

# Rejection feedback carries no transcript or confidence payload.
VOICE_IPC_PREFIX = "__VOICE__:"
_voice_feedback_pending = threading.Event()
_voice_feedback_callback = None
_voice_feedback_stop = threading.Event()
_voice_feedback_thread = None


def set_voice_feedback_callback(callback=None) -> None:
    """Register a non-blocking rejection notification consumer."""
    global _voice_feedback_callback
    _voice_feedback_callback = callback
    _voice_feedback_pending.clear()


def _queue_low_confidence(_event) -> None:
    """Coalesce listener notifications without retaining rejected speech."""
    if not _global_stop_requested:
        _voice_feedback_pending.set()


def _dispatch_voice_feedback() -> None:
    """Deliver pending feedback without diary or database work."""
    if not _voice_feedback_pending.is_set():
        return
    _voice_feedback_pending.clear()
    if _global_stop_requested:
        return
    callback = _voice_feedback_callback
    if callback is not None:
        try:
            callback()
        except Exception as exc:
            debug_log(f"voice feedback callback failed ({type(exc).__name__})", "voice")
    elif os.environ.get("CLAIRE_STDIN_IPC") == "1":
        _emit_ipc_event(VOICE_IPC_PREFIX, "low_confidence", None, "voice")


# ---------------------------------------------------------------------------
# Reminder announcer
#
# The schedule rules (due_reminders/describe_reminder/mark_reminder_fired)
# existed and were tested while nothing in this daemon ever called them, so
# "remind me at 6 to call mum" was booked and silently never spoken.  The
# announcer lives in claire.schedule.announcer; this is the wiring: the shared
# TTS engine supplies both the speaking and the busy check, and start/stop are
# small functions so a test can drive them with a fake engine instead of
# booting the daemon.
# ---------------------------------------------------------------------------

_reminder_announcer: Optional[Any] = None


def _engine_speech_adapter(engine: Any, label: str) -> tuple[Any, Any]:
    """``(speak, busy)`` callables over a TTS *engine*, for either announcer.

    Both loops need the same two answers from the engine, so the adapter is
    written once: ``speak`` reports success honestly (the engine queues and
    returns None, so "spoken" means "the engine was enabled and did not refuse")
    and ``busy`` treats an engine that cannot answer as not speaking.
    """
    def speak(text: str) -> bool:
        """Report success honestly: the engine queues and returns None, so
        "spoken" means "the engine was enabled and did not refuse"."""
        try:
            if getattr(engine, "enabled", True) is False:
                return False
            engine.speak(text)
        except Exception as exc:  # noqa: BLE001 - a raising engine is a failed speak
            debug_log(f"{label} speech failed ({type(exc).__name__})", "schedule")
            return False
        return True

    def busy() -> bool:
        try:
            return bool(engine.is_speaking())
        except Exception:  # noqa: BLE001 - a broken engine is not "speaking"
            return False

    return speak, busy


def _build_reminder_announcer(engine: Any) -> ReminderAnnouncer:
    """A not-yet-started announcer whose speak/busy calls go through *engine*.

    Split out from :func:`_start_reminder_announcer` so a test can exercise the
    engine adapter deterministically, without a background thread racing it.
    """
    speak, busy = _engine_speech_adapter(engine, "reminder")
    return ReminderAnnouncer(speak, busy, poll_interval=REMINDER_POLL_INTERVAL_SEC)


def _start_reminder_announcer(engine: Optional[Any] = None) -> None:
    """Start the schedule-reminder announcer on the shared TTS engine.

    Args:
        engine: TTS engine to speak on.  Defaults to :func:`get_tts_engine`
            (the engine ``main()`` installed); passing one explicitly is what
            lets a test wire a fake engine without booting the daemon.
    """
    global _reminder_announcer
    if _reminder_announcer is not None:
        return
    tts = engine if engine is not None else get_tts_engine()
    if tts is None:
        debug_log("reminder announcer not started: no TTS engine", "schedule")
        return
    announcer = _build_reminder_announcer(tts)
    announcer.start()
    _reminder_announcer = announcer
    # The whole bug hid in silence: name the interval out loud at startup.
    print(f"⏰ Reminder announcer polling every {REMINDER_POLL_INTERVAL_SEC}s", flush=True)
    debug_log("reminder announcer started", "schedule")


def _stop_reminder_announcer() -> None:
    """Stop the announcer, waking its loop instead of waiting an interval."""
    global _reminder_announcer
    announcer, _reminder_announcer = _reminder_announcer, None
    if announcer is not None:
        announcer.stop()
        debug_log("reminder announcer stopped", "schedule")


_timer_announcer: Optional[Any] = None


def _build_timer_announcer(engine: Any) -> TimerAnnouncer:
    """A not-yet-started timer announcer whose speak/busy calls go through *engine*.

    Split out from :func:`_start_timer_announcer` so a test can exercise the
    engine adapter deterministically, without a background thread racing it.
    The manager is the process-wide :func:`default_manager`, the same instance
    the ``timer`` tool writes to, so a timer the owner sets is one this loop
    actually fires.
    """
    speak, busy = _engine_speech_adapter(engine, "timer")
    return TimerAnnouncer(
        speak,
        busy,
        poll_interval=TIMER_POLL_INTERVAL_SEC,
        manager=default_manager(),
    )


def _start_timer_announcer(engine: Optional[Any] = None) -> None:
    """Start the countdown-timer announcer on the shared TTS engine.

    Args:
        engine: TTS engine to speak on.  Defaults to :func:`get_tts_engine`
            (the engine ``main()`` installed); passing one explicitly is what
            lets a test wire a fake engine without booting the daemon.
    """
    global _timer_announcer
    if _timer_announcer is not None:
        return
    tts = engine if engine is not None else get_tts_engine()
    if tts is None:
        debug_log("timer announcer not started: no TTS engine", "schedule")
        return
    announcer = _build_timer_announcer(tts)
    announcer.start()
    _timer_announcer = announcer
    # The whole bug hid in silence: name the precision out loud at startup.
    print(
        f"⏳ Timer announcer armed (fires within {TIMER_POLL_INTERVAL_SEC}s)",
        flush=True,
    )
    debug_log("timer announcer started", "schedule")


def _stop_timer_announcer() -> None:
    """Stop the announcer, waking its loop instead of waiting an interval."""
    global _timer_announcer
    announcer, _timer_announcer = _timer_announcer, None
    if announcer is not None:
        announcer.stop()
        debug_log("timer announcer stopped", "schedule")


# ---------------------------------------------------------------------------
# Control socket
#
# The channel a supervised Claire was missing.  The desktop app can talk to a
# daemon it spawned itself over stdin and stdout.  When systemd runs the daemon
# instead - which is how it runs on the owner's machine - our stdin is
# /dev/null and our stdout belongs to the journal, so an attached UI had nowhere
# to send a chat message, and the app said nothing about it: it enabled the chat
# box, ran the query in its own process, and showed no reply and no error.
#
# The socket carries exactly the lines stdin carries, so the handlers below are
# the stdin handlers, untouched.  Nothing new is accepted, which is the point:
# there is no second protocol to keep in step with the first.
# ---------------------------------------------------------------------------

_control_socket_server: Optional[Any] = None


def _handle_control_line(line: str) -> bool:
    """Handle one control-socket line with the daemon's own stdin handlers.

    Returns whether any handler recognised the line, so the server can log the
    lines that are not part of the protocol instead of silently dropping them.
    A handler that raises is reported and treated as recognised: the line was
    ours, it just did not work, and trying the remaining handlers on it would
    only produce a second, more confusing failure.
    """
    for handler in (
        handle_chat_query_stdin_line,
        handle_chat_cancel_stdin_line,
        handle_chat_new_session_stdin_line,
        handle_chat_rewind_stdin_line,
        handle_chat_restore_stdin_line,
    ):
        try:
            if handler(line):
                return True
        except Exception as exc:  # noqa: BLE001 - one bad line must not drop a client
            debug_log(
                f"control socket handler {handler.__name__} raised "
                f"({type(exc).__name__}: {exc})",
                "chat_ipc",
            )
            return True
    return False


def _build_control_socket_server(path: Optional[str] = None) -> ControlSocketServer:
    """A not-yet-started control socket server, for a test to drive directly."""
    return ControlSocketServer(
        path,
        handle_line=_handle_control_line,
        logger=lambda message: debug_log(message, "chat_ipc"),
    )


def _start_control_socket_server(server: Optional[Any] = None) -> None:
    """Serve the control socket, unless it is switched off.

    Opt out with ``CLAIRE_CONTROL_CHANNEL=0``; relocate it (or keep an isolated
    instance away from the real one) with ``CLAIRE_CONTROL_SOCKET``.
    """
    global _control_socket_server
    if _control_socket_server is not None:
        return
    if os.environ.get("CLAIRE_CONTROL_CHANNEL", "1").strip().lower() in ("0", "false", "no"):
        debug_log("control socket disabled by CLAIRE_CONTROL_CHANNEL", "chat_ipc")
        print("🔌 Control socket disabled by CLAIRE_CONTROL_CHANNEL", flush=True)
        return

    candidate = server if server is not None else _build_control_socket_server()
    try:
        candidate.start()
    except Exception as exc:  # noqa: BLE001 - a missing channel must not stop Claire
        # Say it loudly rather than leave the UI guessing why chat does nothing.
        print(f"⚠️  Control socket unavailable: {exc}", flush=True)
        debug_log(f"control socket failed to start ({type(exc).__name__}: {exc})", "chat_ipc")
        return
    _control_socket_server = candidate
    print(f"🔌 Control socket ready at {candidate.path}", flush=True)
    debug_log("control socket started", "chat_ipc")


def _stop_control_socket_server() -> None:
    """Stop serving and remove our socket file."""
    global _control_socket_server
    server, _control_socket_server = _control_socket_server, None
    if server is not None:
        try:
            server.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            debug_log(f"control socket stop failed ({type(exc).__name__}: {exc})", "chat_ipc")
        debug_log("control socket stopped", "chat_ipc")


def _start_voice_feedback_worker() -> None:
    """Keep rejection delivery independent of synchronous diary processing."""
    global _voice_feedback_thread, _voice_feedback_stop
    if _voice_feedback_thread is not None and _voice_feedback_thread.is_alive():
        return
    _voice_feedback_stop = threading.Event()
    stop = _voice_feedback_stop

    def run():
        while not stop.is_set():
            _voice_feedback_pending.wait()
            if not stop.is_set():
                _dispatch_voice_feedback()

    _voice_feedback_thread = threading.Thread(
        target=run, name="claire-voice-feedback", daemon=True,
    )
    try:
        _voice_feedback_thread.start()
        debug_log("voice feedback worker started", "voice")
    except (RuntimeError, OSError) as exc:
        _voice_feedback_thread = None
        stop.set()
        debug_log(f"voice feedback unavailable ({type(exc).__name__})", "voice")


def _stop_voice_feedback_worker() -> None:
    """Wake and stop notification delivery with a bounded shutdown wait."""
    _voice_feedback_stop.set()
    _voice_feedback_pending.set()
    thread = _voice_feedback_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)
        if thread.is_alive():
            debug_log("voice feedback consumer still finishing after stop", "voice")
    _voice_feedback_pending.clear()


def request_stop() -> None:
    """Request the daemon to stop gracefully."""
    global _global_stop_requested
    _global_stop_requested = True


def set_diary_update_callbacks(
    on_token=None,
    on_status=None,
    on_chunks=None,
    on_complete=None,
) -> None:
    """
    Set callbacks for diary update progress during shutdown.

    These are used by the desktop app to show a live diary update dialog.

    Args:
        on_token: Called with each LLM token as it's generated
        on_status: Called with status messages
        on_chunks: Called with the list of pending conversation chunks
        on_complete: Called when diary update completes (bool = success)
    """
    global _diary_update_callbacks
    _diary_update_callbacks["on_token"] = on_token
    _diary_update_callbacks["on_status"] = on_status
    _diary_update_callbacks["on_chunks"] = on_chunks
    _diary_update_callbacks["on_complete"] = on_complete


def get_pending_diary_chunks() -> list:
    """Get pending conversation chunks from dialogue memory (for UI display only).

    Uses ``get_pending_chunks()`` which discards the atomic snapshot timestamp.
    Do not use the result of this function to drive diary saves — the actual
    save path goes through ``update_diary_from_dialogue_memory``, which calls
    ``get_pending_chunks_with_snapshot()`` internally.
    """
    global _global_dialogue_memory
    if _global_dialogue_memory is None:
        return []
    return _global_dialogue_memory.get_pending_chunks()


def get_hot_window_messages() -> list:
    """Return the current hot-window turns (last ``RECENT_WINDOW_SEC``) as
    ``[{"role": "user"|"assistant", "content": str}, ...]``.

    The chat window seeds its transcript from this on first open so a user who
    has been talking by voice sees recent turns instead of a blank panel. The
    content is already redacted (redaction runs before a turn is added to the
    dialogue memory), so this never leaks raw sensitive input. Returns an empty
    list when the daemon has not booted or the hot window is empty.
    """
    if _global_dialogue_memory is None:
        return []
    return _global_dialogue_memory.get_recent_messages()


def new_chat_session() -> bool:
    """Start a fresh conversation: clear the shared dialogue memory.

    Voice and text share this memory, so a new session resets both. The
    previous conversation is not lost anywhere else — the chat window
    keeps an in-memory archive of its transcript for the session list.
    Nothing is written to disk. Returns False when a query is currently
    running (the engine appends its turns after we clear, resurrecting
    the conversation); the caller should retry after the query finishes.
    """
    global _global_dialogue_memory
    if _global_dialogue_memory is None:
        return False
    if not _chat_query_lock.acquire(blocking=False):
        debug_log("new chat session rejected: a query is in flight", "chat")
        return False
    try:
        _global_dialogue_memory.clear()
        return True
    finally:
        _chat_query_lock.release()


def rewind_chat_to_user(user_index: int) -> bool:
    """Roll the shared dialogue memory back to before a given user turn.

    ``user_index`` is 1-based (the first user message is 1). Every turn
    from that user message on is dropped — including the message itself,
    so the caller can re-submit it and get a fresh reply. Returns True
    when a rewind happened, False when the turn is not in memory or a
    query is currently running (the engine's late turn-append would
    resurrect turns past the rewind point).
    """
    global _global_dialogue_memory
    if _global_dialogue_memory is None:
        return False
    if not _chat_query_lock.acquire(blocking=False):
        debug_log("chat rewind rejected: a query is in flight", "chat")
        return False
    try:
        return _global_dialogue_memory.rewind_before_user_message(user_index)
    finally:
        _chat_query_lock.release()


def set_chat_messages(messages: list) -> bool:
    """Restore an archived session into the shared dialogue memory.

    Used when the chat window switches back to a session from its
    in-memory list. Redaction is applied here, on the daemon side, so the
    diary (written at session end from this memory) never sees raw user
    text even if the window's archive holds it. Returns False when a
    query is currently running (see ``rewind_chat_to_user``).
    """
    global _global_dialogue_memory
    if _global_dialogue_memory is None:
        return False
    if not _chat_query_lock.acquire(blocking=False):
        debug_log("chat restore rejected: a query is in flight", "chat")
        return False
    try:
        from .utils.redact import redact

        scrubbed = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if isinstance(role, str) and isinstance(content, str) and role in ("user", "assistant"):
                scrubbed.append({"role": role, "content": redact(content)})
        _global_dialogue_memory.set_messages(scrubbed)
        return True
    finally:
        _chat_query_lock.release()


# Diary IPC protocol prefix - desktop app intercepts lines starting with this
DIARY_IPC_PREFIX = "__DIARY__:"


def _emit_ipc_event(prefix: str, event_type: str, data, debug_tag: str) -> None:
    """Emit a JSON IPC event line to stdout for the desktop app (subprocess mode).

    Shared by the diary and chat event emitters. Builds ``{"type", "data"}``,
    prints ``{prefix}{json}`` with flush, skips debug logging for ``token``
    events to avoid spam, and swallows+logs emit errors so a bad payload never
    crashes the worker.
    """
    import json
    try:
        event = {"type": event_type, "data": data}
        line = f"{prefix}{json.dumps(event)}"
        print(line, flush=True)
        # An attached UI cannot read that print: under systemd our stdout is the
        # journal's socket, owned by someone else.  The same line goes to the
        # control socket, so a chat window gets its token stream either way.
        server = _control_socket_server
        if server is not None:
            server.broadcast(line)
        if event_type != "token":  # Don't spam for tokens
            debug_log(f"IPC event emitted: {event_type}", debug_tag)
    except Exception as e:
        debug_log(f"IPC emit error: {e}", debug_tag)


def _emit_diary_event(event_type: str, data) -> None:
    """Emit a diary update event to stdout for IPC with the desktop app.

    Used in subprocess mode where callbacks aren't available. The desktop app
    intercepts these lines and forwards them to the diary dialog.

    Args:
        event_type: One of "chunks", "token", "status", "complete"
        data: Event payload (list for chunks, str for token/status, bool for complete)
    """
    _emit_ipc_event(DIARY_IPC_PREFIX, event_type, data, "diary_ipc")


def _emit_chat_event(event_type: str, data) -> None:
    """Emit a chat event to stdout for IPC with the desktop app (subprocess mode).

    The payload never carries unredacted user text: the caller passes the
    already-redacted query to the ``start`` event.
    """
    _emit_ipc_event(CHAT_IPC_PREFIX, event_type, data, "chat_ipc")


def _notify_chat(event_type: str, data, *, callbacks: dict, use_ipc: bool) -> None:
    """Dispatch a chat event to per-call callbacks and/or the IPC stream.

    ``callbacks`` is the dict of caller-supplied callables (``on_start`` etc.),
    not a module global. ``busy`` takes no argument; all others take ``data``.
    """
    callback_map = {
        "start": "on_start",
        "token": "on_token",
        "tool": "on_tool_call",
        "complete": "on_complete",
        "busy": "on_busy",
    }
    callback_name = callback_map.get(event_type)
    if callbacks and callback_name:
        cb = callbacks.get(callback_name)
        if cb is not None:
            try:
                if event_type == "busy":
                    cb()
                else:
                    cb(data)
            except Exception:
                pass
    if use_ipc:
        _emit_chat_event(event_type, data)


@contextlib.contextmanager
def query_lock():
    """Context manager that acquires the shared voice+text query lock (blocking).

    Used by the voice path so a voice query waits for any in-flight text query
    to finish before running ``run_reply_engine`` against the shared dialogue
    memory. The text path uses a non-blocking acquire (reject-with-busy) in
    ``submit_text_query``; the voice path blocks because voice queries should
    not be silently dropped when text is running.
    """
    _chat_query_lock.acquire()
    try:
        yield
    finally:
        _chat_query_lock.release()


def cancel_active_chat_query() -> None:
    """Cancel the in-flight text-chat query, if any.

    Sets the per-query cancellation flag so the chat worker drops the reply
    when ``run_reply_engine`` returns. Distinct from ``request_stop`` (full
    daemon shutdown): this does not stop the voice listener, save the diary,
    or close the database. Used by the chat window's Stop button.
    """
    global _chat_cancel_event
    if _chat_cancel_event is not None:
        _chat_cancel_event.set()
        debug_log("chat query cancellation requested", "chat")


def submit_text_query(
    text: str,
    *,
    on_start=None,
    on_token=None,
    on_tool_call=None,
    on_complete=None,
    on_busy=None,
    use_ipc: bool = False,
    wait_until_free: bool = False,
) -> None:
    """Submit a text query to the reply engine (fire-and-forget).

    Runs ``run_reply_engine`` on a worker thread with ``tts=None`` and the
    shared global dialogue memory, so text and voice are one conversation.
    Results are delivered via the per-call callbacks (bundled mode) and/or
    ``__CHAT__:`` IPC events (subprocess mode). See ``chat_window.spec.md``.

    A second submission while one is running is rejected with a ``busy``
    event rather than queued. A running query can be cancelled with
    ``cancel_active_chat_query``; the reply is then dropped (``complete(None)``)
    rather than displayed.

    ``wait_until_free=True`` changes only that middle sentence: instead of
    reporting ``busy``, this call blocks on the shared query lock and runs when
    the engine is free.  The chat window keeps the default, because there a
    person can see the "busy" and choose; the phone's single dispatcher thread
    passes the flag, because there the alternative is losing what the owner said.
    """
    if not text or not text.strip():
        return

    callbacks = {
        "on_start": on_start,
        "on_token": on_token,
        "on_tool_call": on_tool_call,
        "on_complete": on_complete,
        "on_busy": on_busy,
    }

    dm = _global_dialogue_memory
    cfg = _global_cfg
    db = _global_db
    if dm is None or cfg is None or db is None:
        # Daemon not initialised (e.g. tests that don't boot main()). Fail
        # open with a None complete so the UI doesn't hang.
        _notify_chat("complete", None, callbacks=callbacks, use_ipc=use_ipc)
        return

    if is_stop_requested():
        # Daemon is shutting down. Don't spawn a worker that may use db after
        # close (bundled) or write to a dead pipe (subprocess).
        _notify_chat("complete", None, callbacks=callbacks, use_ipc=use_ipc)
        return

    # One query at a time: voice and text share the lock.  ``wait_until_free``
    # makes this a queue of one rather than a refusal, which is what the phone's
    # dispatcher relies on so a second sentence is answered instead of dropped.
    # The wait is bounded at 60 s (under the phone's 120 s read timeout) so a
    # wedged worker sheds load through the honest busy path instead of hanging
    # the caller forever.
    if wait_until_free:
        if not _chat_query_lock.acquire(timeout=60.0):
            _notify_chat("busy", None, callbacks=callbacks, use_ipc=use_ipc)
            debug_log("chat query timed out waiting for lock", "chat")
            return
    elif not _chat_query_lock.acquire(blocking=False):
        _notify_chat("busy", None, callbacks=callbacks, use_ipc=use_ipc)
        debug_log("chat query rejected: another query is running", "chat")
        return

    # Per-query cancellation flag. The Stop button sets this so the worker
    # drops the reply instead of displaying it.
    global _chat_cancel_event
    cancel_event = threading.Event()
    _chat_cancel_event = cancel_event

    def _worker() -> None:
        try:
            # Snapshot the redacted query for the start event. ``run_reply_engine``
            # redacts internally too; we mirror that here so the IPC stream and
            # the UI never carry raw user text even if the engine hasn't run yet.
            # Done inside the worker's try/except so a redaction failure fails
            # open (complete(None)) and the shared lock is released in finally
            # rather than leaking and blocking every future submission.
            from .utils.redact import redact
            display_query = redact(text)
            _notify_chat("start", display_query, callbacks=callbacks, use_ipc=use_ipc)
            from .reply.engine import run_reply_engine
            reply = run_reply_engine(
                db=db,
                cfg=cfg,
                tts=None,
                text=text,
                dialogue_memory=dm,
                language=None,
                quiet=True,
            )
            if cancel_event.is_set():
                debug_log("chat query cancelled, dropping reply", "chat")
                reply = None
            _notify_chat("complete", reply, callbacks=callbacks, use_ipc=use_ipc)
        except Exception as exc:
            debug_log(f"chat query worker error: {exc}", "chat")
            try:
                _notify_chat("complete", None, callbacks=callbacks, use_ipc=use_ipc)
            except Exception:
                pass
        finally:
            global _chat_cancel_event
            if _chat_cancel_event is cancel_event:
                _chat_cancel_event = None
            _chat_query_lock.release()

    try:
        threading.Thread(target=_worker, name="claire-chat-query", daemon=True).start()
    except Exception:
        # If thread spawning fails, release the lock so future queries work.
        _chat_cancel_event = None
        _chat_query_lock.release()
        _notify_chat("complete", None, callbacks=callbacks, use_ipc=use_ipc)


def handle_chat_query_stdin_line(line: str) -> bool:
    """Parse a stdin line as a chat-query submission (subprocess mode).

    Returns True if the line was a ``__CHAT_QUERY__:`` line and was handled
    (whether or not the query was accepted). Returns False for any other
    line, so the caller can still apply SHUTDOWN / EOF semantics.
    """
    line = line.strip()
    if not line.startswith(CHAT_QUERY_IPC_PREFIX):
        return False
    import json
    try:
        payload = json.loads(line[len(CHAT_QUERY_IPC_PREFIX):])
        text = payload.get("text", "")
    except Exception:
        debug_log("malformed __CHAT_QUERY__ line ignored", "chat_ipc")
        return True
    # Reject non-string payloads (e.g. {"text":[1]}) so a hostile or buggy
    # writer can't crash the stdin monitor via submit_text_query's str API.
    if not isinstance(text, str):
        debug_log("__CHAT_QUERY__ text payload is not a string, ignored", "chat_ipc")
        return True
    # In subprocess mode the reply comes back via __CHAT__: events on stdout.
    submit_text_query(text, use_ipc=True)
    return True


def handle_chat_cancel_stdin_line(line: str) -> bool:
    """Parse a stdin line as a chat-query cancellation (subprocess mode).

    Returns True when the line was a cancel instruction and was handled,
    False for anything else so the caller can apply its own semantics.
    Cancelling with nothing in flight is a no-op, not an error: the user
    can press Stop after the engine has already returned.
    """
    if line.strip() != CHAT_CANCEL_IPC_PREFIX:
        return False
    cancel_active_chat_query()
    return True


def handle_chat_new_session_stdin_line(line: str) -> bool:
    """Parse a stdin line as a chat new-session instruction (subprocess mode).

    Returns True when the line was the new-session instruction and was
    handled, False otherwise so the caller can apply its own semantics.
    """
    if line.strip() != CHAT_NEW_SESSION_IPC_PREFIX:
        return False
    new_chat_session()
    return True


def handle_chat_rewind_stdin_line(line: str) -> bool:
    """Parse a stdin line as a chat rewind instruction (subprocess mode).

    Payload is ``{"user_index": N}`` with N 1-based. Returns True when
    the line was a rewind instruction and was handled (whether or not a
    rewind actually happened), False for anything else.
    """
    line = line.strip()
    if not line.startswith(CHAT_REWIND_IPC_PREFIX):
        return False
    import json
    try:
        payload = json.loads(line[len(CHAT_REWIND_IPC_PREFIX):])
        user_index = int(payload.get("user_index"))
    except Exception:
        debug_log("malformed __CHAT_REWIND__ line ignored", "chat_ipc")
        return True
    if user_index < 1:
        debug_log("__CHAT_REWIND__ user_index out of range, ignored", "chat_ipc")
        return True
    rewind_chat_to_user(user_index)
    return True


def handle_chat_restore_stdin_line(line: str) -> bool:
    """Parse a stdin line as a session-restore instruction (subprocess mode).

    Payload is ``{"messages": [{"role", "content"}, ...]}``. Returns True
    when the line was a restore instruction and was handled (even if the
    payload was malformed), False for anything else.
    """
    line = line.strip()
    if not line.startswith(CHAT_RESTORE_IPC_PREFIX):
        return False
    import json
    try:
        payload = json.loads(line[len(CHAT_RESTORE_IPC_PREFIX):])
        messages = payload.get("messages", [])
    except Exception:
        debug_log("malformed __CHAT_RESTORE__ line ignored", "chat_ipc")
        return True
    if not isinstance(messages, list):
        debug_log("__CHAT_RESTORE__ messages not a list, ignored", "chat_ipc")
        return True
    set_chat_messages(messages)
    return True


def wait_for_chat_worker(timeout_sec: float = 5.0) -> bool:
    """Wait for an in-flight chat worker to finish, bounded.

    Shutdown runs the final diary pass and closes the database. A worker
    that started just before the stop request is still inside
    ``run_reply_engine`` with that connection open, so closing it under
    them raises on a closed SQLite handle, and the diary pass races their
    writes to dialogue memory.

    Returns True when the worker finished (or none was running), False on
    timeout — in which case the caller proceeds anyway rather than
    hanging the quit, which is the lesser of the two failures.
    """
    acquired = _chat_query_lock.acquire(timeout=timeout_sec)
    if acquired:
        _chat_query_lock.release()
        return True
    debug_log(
        f"chat worker still running after {timeout_sec}s, shutting down anyway",
        "chat",
    )
    return False


def is_stop_requested() -> bool:
    """Check if a stop has been requested."""
    return _global_stop_requested


def get_tts_engine():
    """Get the global TTS engine for speaking state polling (used by face widget).

    Never ``None`` once ``main()`` has run: when speech output could not be
    initialised this returns an unavailable stand-in that reports
    ``enabled == False`` and ``is_speaking() == False``, so a poller can call
    those without a guard.
    """
    return _global_tts_engine


def _install_signal_handlers() -> None:
    """Ensure signals like Ctrl+Break trigger clean shutdown."""
    def _raise_keyboard_interrupt(_signum, _frame):
        raise KeyboardInterrupt()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _raise_keyboard_interrupt)
            except Exception:
                pass


def _check_and_update_diary(
    db: Database, cfg, verbose: bool = False, force: bool = False, timeout_sec: Optional[float] = None,
    use_callbacks: bool = False, use_ipc: bool = False
) -> None:
    """Check if diary should be updated and perform batch update if needed.

    Args:
        timeout_sec: Optional override for LLM timeout. If None, uses cfg.llm_chat_timeout_sec.
                    During shutdown, a shorter timeout is used to allow graceful quit.
        use_callbacks: If True, uses the global diary update callbacks for UI updates.
        use_ipc: If True, emits diary events to stdout for IPC with desktop app (subprocess mode).
    """
    global _global_dialogue_memory, _diary_update_callbacks

    debug_log(f"diary update check: force={force}, verbose={verbose}", "memory")

    # Helper to safely call callbacks and/or emit IPC events
    def _notify(event_type: str, data):
        # Map event types to callback names
        callback_map = {"chunks": "on_chunks", "status": "on_status", "token": "on_token", "complete": "on_complete"}
        callback_name = callback_map.get(event_type)

        # Call callback if set (bundled mode)
        if use_callbacks and callback_name and _diary_update_callbacks.get(callback_name):
            try:
                _diary_update_callbacks[callback_name](data)
            except Exception:
                pass

        # Emit IPC event (subprocess mode)
        if use_ipc:
            _emit_diary_event(event_type, data)

    if _global_dialogue_memory is None:
        debug_log("diary update skipped: dialogue_memory is None", "memory")
        _notify("complete", False)
        return

    try:
        should_update = force or _global_dialogue_memory.should_update_diary()
        debug_log(f"diary update: should_update={should_update}, force={force}", "memory")

        if should_update:
            # Display-only: get a snapshot of pending chunks to notify the UI.
            # The atomic snapshot for the actual save is captured inside
            # update_diary_from_dialogue_memory via get_pending_chunks_with_snapshot().
            pending_chunks = _global_dialogue_memory.get_pending_chunks()
            debug_log(f"diary update: found {len(pending_chunks)} pending chunks", "memory")

            if not pending_chunks:
                debug_log("diary update skipped: no pending chunks", "memory")
                _notify("complete", False)
                return

            # Notify about chunks and status
            _notify("chunks", pending_chunks)
            _notify("status", "Writing diary entry...")

            if verbose:
                try:
                    print("📝 Updating your diary. Please wait… (don't press Ctrl+C again)", file=sys.stderr, flush=True)
                except Exception:
                    pass

            source_app = "stdin" if cfg.use_stdin else "voice"
            effective_timeout = timeout_sec if timeout_sec is not None else cfg.llm_chat_timeout_sec

            # Create token handler that notifies via callback and/or IPC
            # For IPC mode, batch tokens to avoid overwhelming the receiver
            token_buffer = []
            last_flush_time = [time.time()]  # Use list for closure mutability
            TOKEN_FLUSH_INTERVAL = 0.1  # Flush every 100ms

            def on_token_handler(token: str):
                if use_callbacks:
                    # Callbacks can handle individual tokens (same process)
                    _notify("token", token)
                elif use_ipc:
                    # IPC mode: batch tokens to reduce event frequency
                    token_buffer.append(token)
                    now = time.time()
                    if now - last_flush_time[0] >= TOKEN_FLUSH_INTERVAL:
                        if token_buffer:
                            _emit_diary_event("token", "".join(token_buffer))
                            token_buffer.clear()
                        last_flush_time[0] = now

            # Only use token handler if we have callbacks or IPC enabled
            on_token = on_token_handler if (use_callbacks or use_ipc) else None

            # Graph best-child picker is a one-digit classification — a fast-tier
            # job, so placement runs on the small model instead of paging in the
            # big chat model for every fact.
            from .llm import resolve_model, Tier
            graph_picker_model = resolve_model(cfg, Tier.FAST)

            summary_id = update_diary_from_dialogue_memory(
                db=db,
                dialogue_memory=_global_dialogue_memory,
                cfg=cfg,
                source_app=source_app,
                voice_debug=cfg.voice_debug,
                timeout_sec=effective_timeout,
                force=force,
                on_token=on_token,
                thinking=getattr(cfg, 'llm_thinking_enabled', False),
                graph_picker_model=graph_picker_model,
            )

            # Flush any remaining tokens in IPC mode
            if use_ipc and token_buffer:
                _emit_diary_event("token", "".join(token_buffer))
                token_buffer.clear()

            if summary_id:
                debug_log(f"diary updated from dialogue memory: id={summary_id}", "memory")
                _notify("complete", True)
            else:
                debug_log("diary update from dialogue memory failed", "memory")
                _notify("complete", False)

            if verbose:
                try:
                    if summary_id:
                        print("✅ Diary update finished.", file=sys.stderr, flush=True)
                    else:
                        print("⚠️ Diary update failed. Shutting down anyway.", file=sys.stderr, flush=True)
                except Exception:
                    pass
        else:
            # No update needed
            _notify("complete", False)
    except Exception as e:
        debug_log(f"diary update check error: {e}", "memory")
        _notify("complete", False)


# --------------------------------------------------------------------------- #
# The phone bridge: the loop's second intake
# --------------------------------------------------------------------------- #

#: How long the phone bridge keeps trying to bind before giving up, and how long
#: it waits between attempts.  The bind is early and cheap, but the address it
#: needs (the tailnet IP) appears late at boot, so this is deliberately generous.
PHONE_BRIDGE_BIND_TIMEOUT_SEC = 180.0
PHONE_BRIDGE_BIND_RETRY_SEC = 5.0

#: The serving bridge, so a reply can be pushed back to the phone. Set by
#: :func:`_start_phone_bridge`; None when the bridge is off.
_phone_bridge: Any = None

#: How long after Claire's last spoken word a phone transcript is still discarded
#: as her own echo.  The phone's audio reaches the bridge over the network a beat
#: after it was captured, and her voice reaches the phone's microphone through the
#: speaker at the same moment it reaches ``is_speaking()``; without a tail window
#: the last words of a sentence land just after ``is_speaking()`` has gone False
#: and are answered as if the owner had said them.
PHONE_ECHO_GRACE_SEC = 1.5

#: What the phone's ambient room sounded like, newest last.  Bounded, and never
#: sent to the reply engine: this is the "keep it, then wait to be called" half of
#: the contract.  The conversation itself lives in the shared dialogue memory, and
#: only an addressed utterance is allowed to write into it.  Kept so the decision
#: is auditable and so a later change can offer the room as context without
#: re-plumbing the sink.
_PHONE_AMBIENT: "deque[str]" = deque(maxlen=8)

#: Guard for the ambient buffer and the queue below.
_phone_lock = threading.Lock()

#: Set while Claire's own voice is audible, so the moment it stops can be stamped.
_phone_was_speaking = False
_phone_speech_ended_at = 0.0

#: Utterances the owner addressed to Claire that the reply engine had not started
#: on yet, oldest first.  Bounded: an unbounded queue against a hung engine is a
#: memory leak, and a cap this small is still far more than a person speaks while
#: waiting for one answer.
PHONE_QUEUE_MAX = 16
_phone_queue: "deque[str]" = deque()
_phone_dispatching = False


def phone_utterance_decision(
    text: str,
    *,
    kind: str,
    speaking: bool,
    seconds_since_speech: Optional[float],
    wake_word: str,
    aliases: Any,
    fuzzy_ratio: float,
) -> str:
    """Decide what to do with one phone utterance, as a pure function.

    Returns ``"answer"`` (it was addressed to Claire), ``"ambient"`` (it was the
    room, so keep it and do not reply) or ``"echo"`` (it was Claire's own voice
    coming back through the phone's microphone).  Split out from the sink because
    this is the decision the feedback loop turned on, and a pure function can be
    tested at every corner of it without a database, a model or a socket.

    ``kind`` is the bridge's own tag: ``"text"`` for ``POST /text``, ``"transcript"``
    for ``POST /transcribe``.  A typed sentence is addressed by construction -- no
    microphone was involved, so it cannot be Claire's voice returning, and typing
    into the phone is as deliberate as typing into the laptop's chat window, which
    has never needed a wake word.  Ambient audio is the opposite case, and it is
    the path the loop ran through, so the wake word is required there and only
    there.
    """
    if kind == "text":
        return "answer"
    if speaking:
        return "echo"
    if seconds_since_speech is not None and seconds_since_speech < PHONE_ECHO_GRACE_SEC:
        return "echo"
    lowered = (text or "").strip().lower()
    names = {str(wake_word or "").strip().lower()}
    for alias in aliases or ():
        alias = str(alias or "").strip().lower()
        if alias:
            names.add(alias)
    names.discard("")
    if not names:
        # Misconfigured to the point of having no name at all: refusing to answer
        # is the honest failure, because answering everything is the bug.
        return "ambient"
    primary = str(wake_word or "").strip().lower() or sorted(names)[0]
    if is_wake_word_detected(lowered, primary, sorted(names), fuzzy_ratio):
        return "answer"
    return "ambient"


def _phone_speech_state() -> tuple:
    """Whether Claire is speaking now, and how long ago she stopped.

    The TTS engine is asked, never assumed: an engine that could not start reports
    ``is_speaking() == False`` honestly, and this must not treat that as "audible".
    """
    global _phone_was_speaking, _phone_speech_ended_at
    tts = get_tts_engine()
    speaking = False
    if tts is not None:
        try:
            speaking = bool(tts.is_speaking())
        except Exception:  # noqa: BLE001 - a broken engine is not "speaking"
            speaking = False
    now = time.monotonic()
    with _phone_lock:
        if speaking:
            _phone_was_speaking = True
            _phone_speech_ended_at = 0.0
        elif _phone_was_speaking:
            _phone_was_speaking = False
            _phone_speech_ended_at = now
        ended = _phone_speech_ended_at
    if speaking:
        return True, None
    if not ended:
        return False, None
    return False, now - ended


def _phone_enqueue(text: str) -> None:
    """Put an addressed utterance on the phone queue and make sure it drains.

    Exactly one dispatcher thread exists at a time.  It blocks on the shared query
    lock rather than probing it, so a second thing the owner says while Claire is
    answering the first is *waiting*, not refused -- the previous behaviour told
    them to "send that again in a moment", which loses the words.
    """
    global _phone_dispatching
    text = str(text or "").strip()
    if not text:
        return
    with _phone_lock:
        if len(_phone_queue) >= PHONE_QUEUE_MAX:
            debug_log("phone queue full: refusing the newest utterance", "phone")
            return
        _phone_queue.append(text)
        start = not _phone_dispatching
        if start:
            _phone_dispatching = True
    if start:
        threading.Thread(
            target=_phone_drain_queue, name="claire-phone-queue", daemon=True
        ).start()


def _phone_drain_queue() -> None:
    """Answer queued phone utterances in order, one at a time."""
    global _phone_dispatching
    while True:
        with _phone_lock:
            if not _phone_queue:
                _phone_dispatching = False
                return
            text = _phone_queue.popleft()
        try:
            _phone_answer(text)
        except Exception as exc:  # noqa: BLE001 - one bad turn must not stop the queue
            debug_log(f"phone queue turn failed ({type(exc).__name__})", "phone")


def _phone_answer(text: str) -> None:
    """Run one phone utterance through the shared reply engine."""
    bridge = _phone_bridge

    def on_complete(reply: Any) -> None:
        """Queue Claire's answer for the phone's next ``GET /reply``.

        ``_notify_chat`` passes the payload straight through (``cb(data)``), and
        for the ``complete`` event that payload is the reply TEXT - the string
        ``run_reply_engine`` returned - or None when the call failed.  It is not
        a success flag; reading it as one silently queues nothing, which is
        exactly the bug this docstring exists to stop anyone repeating.
        """
        if bridge is None:
            return
        answer = str(reply).strip() if reply is not None else ""
        if answer:
            bridge.push_reply(answer)
            debug_log(f"phone reply queued ({len(answer)} chars)", "phone")
        else:
            # Say so on the phone rather than leaving the owner waiting on a reply
            # that is never coming.
            bridge.push_reply(
                "[Claire could not answer that - the model call failed or returned nothing.]"
            )

    def on_busy() -> None:
        # Unreachable through the queue (the dispatcher waits for the lock), kept
        # as the honest report if that ever stops being true.
        if bridge is not None:
            bridge.push_reply(
                "[Claire is already answering something else; send that again in a moment.]"
            )

    debug_log(f"phone utterance accepted ({len(text)} chars)", "phone")
    submit_text_query(
        text, on_complete=on_complete, on_busy=on_busy, wait_until_free=True
    )


def _phone_utterance_sink(utterance: Any) -> None:
    """Decide what one phone utterance is, and answer only what was addressed.

    This is the sink the bridge's ``sink_unconfigured`` error asks for.  What it
    accepts is routed through :func:`submit_text_query`, which runs the shared
    reply engine and the shared dialogue memory - so a sentence typed on the phone
    and a sentence spoken into the microphone are ONE conversation, not two.

    What it refuses is the point.  The phone's microphone is always open, so
    ``/transcribe`` carries the whole room, including Claire's own voice coming
    out of the phone's speaker.  Every one of those used to be sent straight to
    the reply engine, so she heard herself, answered, heard the answer, and
    answered again - indefinitely.  Now an ambient utterance that does not name
    her is kept (bounded, in :data:`_PHONE_AMBIENT`) and never answered, and an
    utterance that arrives while she is speaking, or within
    :data:`PHONE_ECHO_GRACE_SEC` of her last word, is dropped as echo without even
    being kept, because it is her own voice rather than the room.

    The gate is :func:`is_wake_word_detected` - the loop's own matcher, not a
    second one - so the phone and the laptop wake her on exactly the same words,
    with exactly the same tolerance for a mishearing.

    An addressed utterance goes on the queue (:func:`_phone_enqueue`) instead of
    being refused when the engine is busy, because "send that again in a moment"
    throws the owner's words away.

    The utterance text is deliberately not logged: it is the owner's speech, and
    the daemon log is not the place for it.  Only its length is.
    """
    utterance = utterance or {}
    text = str(utterance.get("text") or "").strip()
    if not text:
        return

    cfg = _global_cfg
    kind = str(utterance.get("kind") or "transcript").strip().lower()
    speaking, since = _phone_speech_state()
    verdict = phone_utterance_decision(
        text,
        kind=kind,
        speaking=speaking,
        seconds_since_speech=since,
        wake_word=str(getattr(cfg, "wake_word", "claire") or "claire"),
        aliases=getattr(cfg, "wake_aliases", None),
        fuzzy_ratio=float(getattr(cfg, "wake_fuzzy_ratio", 0.82) or 0.82),
    )

    if verdict == "echo":
        # Claire's own voice, not the room: do not keep it either, or the next
        # addressed question would be answered against a transcript of her.
        debug_log(f"phone transcript dropped as echo ({len(text)} chars)", "phone")
        return

    if verdict == "ambient":
        with _phone_lock:
            _PHONE_AMBIENT.append(text)
        debug_log(
            f"phone ambient utterance kept, not answered ({len(text)} chars)", "phone"
        )
        return

    if kind == "text":
        debug_log(f"phone typed utterance accepted ({len(text)} chars)", "phone")
    else:
        debug_log(f"phone utterance addressed to Claire ({len(text)} chars)", "phone")

    _phone_enqueue(text)


def _warm_phone_recogniser(host: str, port: int, token: str) -> None:
    """Pay the recogniser's one-time cost at startup instead of on the owner.

    Measured with a real 3.6 s utterance against the live bridge:

        first call after a fresh start : 21.94 s   (0.2x realtime)
        second call                    :  0.07 s   (52.7x realtime)

    So the model is not slow - building it is, and it used to happen on the
    owner's first sentence, which the phone experiences as a ~22 s hang because
    its read timeout is 120 s.  One dummy transcribe at startup moves that cost
    where nobody is waiting for it.
    """
    import http.client
    import io
    import json
    import time
    import wave

    # 0.2 s of silence at 16 kHz mono 16-bit - the smallest thing that is still a
    # real WAV, so it exercises the same route a real utterance takes.
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 3200)
    body = buf.getvalue()

    started = time.time()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=300)
        conn.request(
            "POST",
            "/transcribe",
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "audio/wav",
                "Content-Length": str(len(body)),
            },
        )
        response = conn.getresponse()
        response.read()
        elapsed = time.time() - started
        if response.status == 200:
            debug_log(
                f"phone bridge warmed the recogniser in {elapsed:.1f}s "
                "(the first phone utterance no longer pays it)",
                "phone",
            )
        else:
            debug_log(
                f"phone bridge warm-up returned {response.status} after "
                f"{elapsed:.1f}s; the first phone utterance will pay the cost",
                "phone",
            )
        conn.close()
    except Exception as exc:  # noqa: BLE001 - warming must never be fatal
        debug_log(
            f"phone bridge warm-up failed ({type(exc).__name__}); the first phone "
            "utterance will pay the cost",
            "phone",
        )


def _start_phone_bridge(listener: Any = None) -> None:
    """Serve the bridge from THIS process, wired to THIS loop.

    Run separately (``python -m claire.phone serve``) the bridge has no loop to
    deliver into and can only answer "no utterance sink is wired up" - which is
    exactly what the owner hit.  Started here, its sink is the running daemon, so
    a phone utterance reaches the same conversation as speech.

    ``listener`` is the running VoiceListener, and it supplies the recogniser: the
    bridge is given the SAME authoritative engine the voice loop uses, not a
    second one.  Left to itself the bridge builds its own Parakeet, and two NeMo
    instances on one GPU is not a doubling of memory but a CUDA fault -
    ``an illegal memory access was encountered`` - which made every
    ``/transcribe`` return 502 while voice carried on working.

    Opt out with ``CLAIRE_PHONE_BRIDGE=0``.  A non-loopback bind additionally
    requires ``CLAIRE_PHONE_ALLOW_PUBLIC=1``: reaching the bridge from a phone
    means listening on the tailnet, and opening a port to a whole network must
    never be something that happens by accident.
    """
    if os.environ.get("CLAIRE_PHONE_BRIDGE", "1").strip().lower() in ("0", "false", "no"):
        debug_log("phone bridge disabled by CLAIRE_PHONE_BRIDGE", "phone")
        return

    host = (os.environ.get("CLAIRE_PHONE_HOST") or "").strip() or None
    port_text = (os.environ.get("CLAIRE_PHONE_PORT") or "").strip()
    port = int(port_text) if port_text.isdigit() else None
    allow_public = (
        os.environ.get("CLAIRE_PHONE_ALLOW_PUBLIC", "0").strip().lower()
        in ("1", "true", "yes")
    )

    def _serve() -> None:
        """Bind, serve, then warm - retrying the bind until the network is up.

        The bind has to wait for the tailnet address to exist.  At boot Claire
        starts from ``default.target`` while Tailscale is still coming up, and a
        bind to an address that is not there yet fails immediately - which is
        exactly how the bridge bound fine on a restart but was missing after a
        reboot.  So keep trying for a couple of minutes instead of giving up on
        the first attempt.
        """
        import time as _time

        from .phone.bridge import PhoneBridge, create_server

        transcriber_factory = None
        if listener is not None:
            # Reuse the loop's authoritative recogniser rather than building a
            # second one. Private on purpose: it is the engine's single owner,
            # and reaching it any other way would create a rival instance.
            transcriber_factory = getattr(
                listener, "_get_authoritative_stt_engine", None
            )

        bridge = PhoneBridge(
            sink=_phone_utterance_sink, transcriber_factory=transcriber_factory
        )

        deadline = _time.monotonic() + PHONE_BRIDGE_BIND_TIMEOUT_SEC
        attempt = 0
        server = None
        while True:
            attempt += 1
            try:
                server = create_server(
                    bridge, host=host, port=port, allow_public_bind=allow_public
                )
                break
            except Exception as exc:  # noqa: BLE001 - retry, then give up honestly
                if _time.monotonic() >= deadline:
                    debug_log(
                        f"phone bridge gave up binding after {attempt} attempts "
                        f"({type(exc).__name__}: {exc}); voice is unaffected",
                        "phone",
                    )
                    return
                if attempt == 1:
                    debug_log(
                        f"phone bridge waiting for the network to accept a bind "
                        f"({type(exc).__name__}); retrying",
                        "phone",
                    )
                _time.sleep(PHONE_BRIDGE_BIND_RETRY_SEC)

        global _phone_bridge
        _phone_bridge = bridge
        threading.Thread(
            target=server.serve_forever, name="claire-phone-bridge", daemon=True
        ).start()
        bound_host, bound_port = server.server_address[0], server.server_address[1]
        print(
            f"\U0001f4f1 Phone bridge listening on http://{bound_host}:{bound_port}",
            flush=True,
        )
        debug_log(f"phone bridge serving on {bound_host}:{bound_port}", "phone")
        try:
            token = bridge.token()
        except Exception as exc:  # noqa: BLE001
            debug_log(
                f"phone bridge warm-up skipped, no token ({type(exc).__name__})",
                "phone",
            )
            return
        _warm_phone_recogniser(bound_host, bound_port, token)

    threading.Thread(
        target=_serve, name="claire-phone-serve", daemon=True
    ).start()


def _apply_cli_overrides(cfg: Settings, *, no_tune: bool = False) -> Settings:
    """Return ``cfg`` with this run's command-line choices applied.

    ``Settings`` is frozen, so a flag produces a *new* config rather than
    mutating a shared one: nothing further down can be surprised by a value
    changing under it, and a flag cannot leak into a code path that reads the
    settings object it was handed earlier. A test that wants the pinging
    thinking tune gone can also set ``CLAIRE_TUNE_ENABLED=0``, which is what
    the systemd unit's environment file is for.
    """
    if no_tune and cfg.tune_enabled:
        debug_log("--no-tune: the thinking tune is silenced for this run", "voice")
        return replace(cfg, tune_enabled=False)
    return cfg


def main(smoke_test: bool = False, no_tune: bool = False) -> None:
    """Main daemon entry point.

    Args:
        smoke_test: If True, initialise all components, print a success
            marker, and return without entering the main event loop.
            Used by CI smoke tests to verify the build is not broken.
        no_tune: If True, silence the thinking tune for this run
            (``python -m claire --no-tune``).
    """
    global _global_dialogue_memory, _global_stop_requested, _global_tts_engine
    global _warm_profile_graph_listener

    # Reset stop flag at start (in case of restart)
    _global_stop_requested = False
    _voice_feedback_pending.clear()

    _install_signal_handlers()

    cfg = _apply_cli_overrides(load_settings(), no_tune=no_tune)
    db = Database(cfg.db_path, cfg.sqlite_vss_path)
    # Expose cfg + db so the text-chat submission path shares the same store
    # and config as the voice listener (one conversation, one config).
    global _global_cfg, _global_db
    _global_cfg = cfg
    _global_db = db

    debug_log("daemon started", "claire")
    print("✓ Daemon started", flush=True)
    print(f"🧠 Using chat model: {cfg.llm_chat_model}", flush=True)
    # Name the recogniser that actually transcribes turns. This banner first
    # said "Using whisper model: <name>" unconditionally, then promised
    # "(whisper <model> is the degraded fallback)". Both are false now that
    # Whisper is opt-in only (decision D4: one listening model): with Parakeet
    # configured nothing imports or loads a second engine, so advertising a
    # Whisper fallback would be a lie printed at every boot. Whisper is named
    # only when it is the explicitly configured recogniser.
    _stt_name = str(getattr(cfg, "stt_engine", "") or "parakeet").strip().lower()
    if _stt_name in ("whisper", "faster_whisper", "faster-whisper"):
        print(f"🎤 Using whisper model: {cfg.whisper_model}", flush=True)
    else:
        print(f"🎤 Authoritative recogniser: {_stt_name or 'parakeet'} "
              f"(the one listening model; no fallback engine)", flush=True)

    # MCP preflight: discover and cache external MCP tools
    mcps = getattr(cfg, "mcps", {}) or {}
    if mcps:
        print(f"📡 Discovering MCP tools from {len(mcps)} server(s)...", flush=True)
        try:
            mcp_tools, mcp_errors = initialize_mcp_tools(mcps, verbose=False)

            # Group tools by server for display
            tools_by_server: dict = {}
            for tool_name in mcp_tools.keys():
                if "__" in tool_name:
                    server_name = tool_name.split("__")[0]
                    if server_name not in tools_by_server:
                        tools_by_server[server_name] = []
                    tools_by_server[server_name].append(tool_name)

            for server_name in mcps.keys():
                count = len(tools_by_server.get(server_name, []))
                if count > 0:
                    print(f"  ✅ {server_name}: {count} tools available", flush=True)
                elif server_name in mcp_errors:
                    print(f"  ❌ {server_name}: {mcp_errors[server_name]}", flush=True)
                else:
                    print(f"  ⚠️ {server_name}: no tools discovered", flush=True)

            debug_log(f"MCP tools cached: {len(mcp_tools)} total", "mcp")
        except Exception as e:
            debug_log(f"MCP discovery failed: {e}", "mcp")
            print(f"  ⚠️ MCP discovery failed: {e}", flush=True)
    else:
        print("📡 No MCP servers configured", flush=True)

    # Initialize dialogue memory with timeout
    print("💾 Initializing dialogue memory...", flush=True)
    _global_dialogue_memory = DialogueMemory(
        inactivity_timeout=cfg.dialogue_memory_timeout,
        max_interactions=20
    )
    print("✓ Dialogue memory initialized", flush=True)

    # Wire the conversation-scoped warm-profile cache to graph mutations.
    # When the User or Directives branch is mutated mid-conversation, the
    # cached warm profile is dropped so the next reply rebuilds it from
    # the current graph state. World-branch writes (typical webSearch
    # extractions) do not touch warm profile, so they are ignored.
    try:
        from .memory.graph import (
            BRANCH_DIRECTIVES,
            BRANCH_USER,
            register_graph_mutation_listener,
        )

        _wp_relevant_branches = {BRANCH_USER, BRANCH_DIRECTIVES}

        # Read the DialogueMemory ref through the module global at fire
        # time, not via closure capture, so a future singleton swap (tests
        # or hot-reload) routes invalidation to the live instance instead
        # of the freed one.
        def _invalidate_wp_on_graph_mutation(*, action, node_id, branch):
            del action, node_id  # Only the branch matters for warm-profile filtering.
            if branch not in _wp_relevant_branches:
                return
            dm = _global_dialogue_memory
            if dm is None:
                return
            try:
                dm.invalidate_warm_profile()
                debug_log(
                    f"warm profile invalidated by {branch} graph mutation",
                    "memory",
                )
            except Exception as exc:
                debug_log(
                    f"warm profile invalidation failed (non-fatal): {exc}",
                    "memory",
                )

        # If a previous run left a listener registered (re-entry without
        # full process restart), drop it before installing the new one so
        # the registry never accumulates stale closures.
        if _warm_profile_graph_listener is not None:
            try:
                from .memory.graph import unregister_graph_mutation_listener
                unregister_graph_mutation_listener(_warm_profile_graph_listener)
            except Exception:
                pass
        register_graph_mutation_listener(_invalidate_wp_on_graph_mutation)
        _warm_profile_graph_listener = _invalidate_wp_on_graph_mutation
    except Exception as exc:
        debug_log(
            f"warm profile mutation listener wiring failed (non-fatal): {exc}",
            "memory",
        )

    # Knowledge graph: wipe + re-seed if the on-disk shape predates the
    # User/Directives/World taxonomy. Non-destructive to the diary —
    # users can re-import via the memory viewer.
    try:
        from .memory.graph import GraphMemoryStore
        _graph_store_boot = GraphMemoryStore(cfg.db_path)
        if _graph_store_boot.migrate_legacy_shape():
            print("🧹 Wiped legacy knowledge graph; re-seeded User / Directives / World branches", flush=True)
            print("   📥 Open the memory viewer and use 'Import from Diary' to repopulate.", flush=True)
        _graph_store_boot.close()
    except Exception as e:
        debug_log(f"graph legacy-shape migration failed (non-fatal): {e}", "memory")

    # Location status. IP/UPnP/GeoLite2 detection was removed, so the only
    # possible source is a city the user configured explicitly. Say plainly
    # which of the three states we are in; never probe the network here.
    location_context = get_location_context(cfg)
    if location_context == "Location: Disabled":
        print("📍 Location services disabled", flush=True)
    elif location_context == "Location: Unknown":
        print(
            "📍 No location configured (set 'location_city' in config.json)",
            flush=True,
        )
    else:
        print(f"📍 {location_context}", flush=True)

    # Initialize TTS. A failure here (an unknown engine name, a missing voice
    # model, a missing key) disables speech output; it must not take the whole
    # daemon down. ``_initialize_tts_engine`` reports the reason and returns
    # None, which becomes an explicitly unavailable engine reference - but it
    # cannot see every failure: the Fish engine builds its client lazily, so a
    # missing/unreadable key file or an absent voice id raises from start()
    # below. Either way speech goes unavailable and Claire keeps running.
    tts = _initialize_tts_engine(cfg)
    if tts is None:
        tts = _UnavailableTTS()
    if not _start_tts_engine(tts) and not isinstance(tts, _UnavailableTTS):
        try:
            tts.stop()
        except Exception as exc:
            debug_log(f"TTS engine cleanup after failed start failed: {exc}", "tts")
        # The listener, the face-widget poller and the text path all consume
        # this reference: an inert stand-in reports "nothing spoken" instead of
        # re-raising on every reply.
        tts = _UnavailableTTS()
    _global_tts_engine = tts  # Expose for face widget speaking animation

    # Initialize voice listening (only if dependencies available)
    print("🎤 Preparing speech recognition in the background...", flush=True)
    voice_thread: Optional[threading.Thread] = None
    voice_thread = VoiceListener(
        db, cfg, tts, _global_dialogue_memory,
        on_low_confidence=_queue_low_confidence,
    )
    voice_thread.start()

    # The phone's intake. Wired AFTER the listener so the sink reaches a live
    # loop; it is a background thread, so a bridge that cannot bind (or a
    # recogniser that cannot load) never delays or breaks voice.
    _start_phone_bridge(voice_thread)

    if smoke_test:
        print("SMOKE_TEST_INIT_OK", flush=True)
        debug_log("smoke test: all components initialised successfully", "claire")

        # Clean shutdown: stop engines, close database, tear down MCP runtime.
        # The caller is responsible for printing SMOKE_TEST_PASSED / FAILED.
        if voice_thread is not None:
            try:
                voice_thread.stop()
                voice_thread.join(timeout=2.0)
            except Exception:
                pass

        if tts is not None:
            try:
                tts.stop()
            except Exception:
                pass

        try:
            from .tools.external.mcp_runtime import shutdown_runtime
            shutdown_runtime()
        except Exception:
            pass

        db.close()

        if _warm_profile_graph_listener is not None:
            try:
                from .memory.graph import unregister_graph_mutation_listener
                unregister_graph_mutation_listener(_warm_profile_graph_listener)
            except Exception:
                pass
            _warm_profile_graph_listener = None

        # Reset module-level globals so in-process re-entry is clean.
        _global_dialogue_memory = None
        _global_tts_engine = None

        return

    _start_voice_feedback_worker()
    # Behind the smoke-test early return on purpose: CI boots must not spawn
    # a reminder thread, real runs must (this feature was invisible once).
    _start_reminder_announcer(tts)
    # Beside the reminder loop and behind the same smoke-test early return: a
    # timer due during a CI boot must not be spoken, and the loop is useless
    # without the owner listening.
    _start_timer_announcer(tts)
    # Beside the announcers and behind the same smoke-test early return: a CI
    # boot has no UI to serve, and a second server in the test box would only
    # fight the developer's own socket for the path.
    _start_control_socket_server()

    # Periodic diary update checking
    last_diary_check = time.time()
    diary_check_interval = 60.0

    # Start stdin monitor thread.
    # Two jobs:
    #   1. Windows shutdown signal: CTRL_BREAK_EVENT doesn't work reliably with
    #      CREATE_NO_WINDOW, so we treat stdin EOF / a bare "SHUTDOWN" line as a
    #      stop request (unchanged behaviour).
    #   2. Subprocess chat query-in: the desktop app writes
    #      ``__CHAT_QUERY__:{"text":"..."}`` lines so the chat window can submit
    #      text when the daemon runs as a separate process. Non-chat lines are
    #      ignored so the monitor is a no-op for users who never open the chat.
    def stdin_monitor():
        try:
            # When parent closes our stdin, readline returns empty
            while True:
                line = sys.stdin.readline()
                if not line:  # EOF - stdin closed
                    debug_log("stdin closed, requesting stop", "claire")
                    request_stop()
                    break
                stripped = line.strip()
                if stripped == "SHUTDOWN":
                    debug_log("SHUTDOWN command received, requesting stop", "claire")
                    request_stop()
                    break
                # Chat query-in (subprocess mode). Returns False for any other
                # line, which we silently ignore.
                if handle_chat_cancel_stdin_line(stripped):
                    continue
                if handle_chat_new_session_stdin_line(stripped):
                    continue
                if handle_chat_rewind_stdin_line(stripped):
                    continue
                if handle_chat_restore_stdin_line(stripped):
                    continue
                if stripped.startswith(CHAT_QUERY_IPC_PREFIX):
                    handle_chat_query_stdin_line(stripped)
        except Exception:
            pass  # stdin might not be available

    # Run the monitor on Windows (shutdown signal) and whenever the desktop
    # app explicitly signals it owns our stdin (subprocess chat query-in on
    # any platform). The desktop app sets CLAIRE_STDIN_IPC=1 when spawning us
    # so that a bare ``python -m claire.main < /dev/null`` (or a systemd unit
    # with StandardInput=null) does NOT start the monitor and immediately exit
    # on EOF. Bundled mode uses a QThread, not a subprocess, so it's skipped.
    _start_stdin_monitor = (
        (sys.platform == "win32" and not getattr(sys, 'frozen', False))
        or (
            not getattr(sys, 'frozen', False)
            and os.environ.get("CLAIRE_STDIN_IPC") == "1"
        )
    )
    if _start_stdin_monitor:
        stdin_thread = threading.Thread(target=stdin_monitor, daemon=True)
        stdin_thread.start()

    try:
        # Main daemon loop
        while not _global_stop_requested:
            time.sleep(1.0)
            now = time.time()

            # Periodically check if diary should be updated. The diary is
            # delay-tolerant background work: if a query holds the single-writer
            # lock, skip this tick and defer to the next 60 s cycle rather than
            # serialising a multi-minute large-model summarisation behind a
            # user-visible reply (or contending for the same chat model/VRAM).
            if now - last_diary_check >= diary_check_interval:
                if _chat_query_lock.acquire(blocking=False):
                    try:
                        _check_and_update_diary(db, cfg, verbose=False)
                    finally:
                        _chat_query_lock.release()
                last_diary_check = now

        # Keep voice thread alive (unless stop requested)
        if voice_thread is not None:
            while voice_thread.is_alive() and not _global_stop_requested:
                time.sleep(0.5)
                if _chat_query_lock.acquire(blocking=False):
                    try:
                        _check_and_update_diary(db, cfg, verbose=False)
                    finally:
                        _chat_query_lock.release()

    except KeyboardInterrupt:
        debug_log("daemon received KeyboardInterrupt", "claire")
    finally:
        _stop_voice_feedback_worker()
        _stop_reminder_announcer()
        _stop_timer_announcer()
        _stop_control_socket_server()
        print("🔄 Daemon shutting down - saving memory...", flush=True)
        debug_log("daemon finally block starting - performing cleanup", "claire")

        if voice_thread is not None:
            debug_log("stopping voice thread...", "claire")
            voice_thread.stop()
            try:
                voice_thread.join(timeout=2.0)
            except Exception:
                pass
            debug_log("voice thread stopped", "claire")

        # A chat worker that started just before the stop request is still
        # inside run_reply_engine, holding the database connection the
        # diary pass below is about to use and the one db.close() will
        # shut. Wait for it, bounded: quitting a moment late beats a
        # closed-handle raise or a diary pass racing its writes.
        wait_for_chat_worker(timeout_sec=5.0)

        # Final diary update before shutdown
        debug_log("performing final diary update (force=True)...", "claire")
        print("📝 Updating diary before shutdown...", flush=True)

        # Check dialogue memory status
        if _global_dialogue_memory is None:
            print("⚠️ Dialogue memory is None - nothing to save", flush=True)
        else:
            # Display-only count; actual save uses the atomic snapshot path.
            pending = _global_dialogue_memory.get_pending_chunks()
            print(f"💬 Found {len(pending)} pending conversation chunks", flush=True)

        # Use callbacks if they were set by desktop app (for live UI updates in bundled mode)
        # Use IPC (stdout events) if callbacks not set (subprocess mode)
        use_callbacks = any(_diary_update_callbacks.values())
        use_ipc = not use_callbacks  # Subprocess mode - emit events to stdout
        _check_and_update_diary(db, cfg, verbose=True, force=True, timeout_sec=SHUTDOWN_DIARY_TIMEOUT_SEC, use_callbacks=use_callbacks, use_ipc=use_ipc)
        print("✅ Diary update complete", flush=True)
        debug_log("diary update complete", "claire")

        if tts is not None:
            tts.stop()

        # Tear down persistent MCP sessions so subprocess-launched
        # children (e.g. chrome-devtools-mcp's Chrome) close cleanly.
        try:
            from .tools.external.mcp_runtime import shutdown_runtime
            shutdown_runtime()
        except Exception as _e:
            debug_log(f"MCP runtime shutdown error: {_e}", "claire")

        db.close()

        # Drop the warm-profile graph listener so the module registry does
        # not retain a closure pointing at this run's DialogueMemory after
        # shutdown — relevant for tests and any embedder that re-runs the
        # daemon in-process.
        if _warm_profile_graph_listener is not None:
            try:
                from .memory.graph import unregister_graph_mutation_listener
                unregister_graph_mutation_listener(_warm_profile_graph_listener)
            except Exception:
                pass
            _warm_profile_graph_listener = None

        debug_log("daemon stopped", "claire")
        print("👋 Daemon stopped", flush=True)


if __name__ == "__main__":
    import sys as _sys
    smoke_test = "--smoke-test" in set(_sys.argv[1:])
    main(smoke_test=smoke_test)
