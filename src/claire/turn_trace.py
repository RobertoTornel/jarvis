"""Per-turn latency tracing: one structured record per answered turn.

Answering "why was that turn slow?" used to mean writing a bespoke probe
script, and every number in this project's latency history was produced that
way. This module is the queryable form of the same measurement: a
:class:`TurnTrace` created once per turn, stamped once at each stage, and
written as one JSON object per turn to a single bounded append-only file.

Stages (see :data:`CANONICAL_STAGES`) are stamped with ``time.monotonic()``,
never wall-clock, so a stage duration survives an NTP step. Stored as offsets
in seconds from the record's ``started_monotonic``; adding the two gives the
absolute monotonic timestamp back.

**Privacy.** The record carries lengths and flags only. The question and the
answer text are added *only* when the existing ``voice_debug`` setting is on
(``claire.debug.debug_enabled``); with it off there is no text in the file at
all. ``cache_hit`` defaults to ``None`` — "unknown" — because the gateway has
no cache marker yet (work item 7.4); it is never filled with a guessed
``True``/``False``.

**Never blocks a turn.** ``finish`` hands the record to one background writer
thread through a bounded queue and returns; the file is one line per turn and
is rotated at :data:`MAX_TRACE_FILE_BYTES` into a single ``.1`` backup, so the
on-disk pair is bounded rather than growing for ever.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .debug import debug_enabled, debug_log

#: The stages the contract names, in the order they occur on the real voice
#: path. The handover lists "first audio byte, and speech start"; on the wire
#: the listener calls ``speak()`` (``speech_start``) and the TTS worker writes
#: the first device byte (``first_audio_byte``) afterwards, so the two audio
#: stamps are ordered as they actually happen rather than as the page lists
#: them. The *names* are the contract's.
CANONICAL_STAGES: Tuple[str, ...] = (
    "transcription_end",
    "router_start",
    "router_end",
    "planner_start",
    "planner_end",
    "llm_first_token",
    "llm_last_token",
    "speech_start",
    "first_audio_byte",
)

#: Stages after which a turn can be written. Everything up to and including
#: ``llm_last_token`` is stamped inside the reply engine; the two audio stages
#: are stamped afterwards, by the listener and the TTS playback thread.
#: ``transcription_end`` is stamped by the listener before the engine's trace
#: exists, and only on the voice path — a text turn has none.
_ENGINE_STAGES = frozenset(CANONICAL_STAGES[1:7])

#: One file is rotated once it passes this many bytes; the rotation keeps a
#: single ``turns.jsonl.1`` backup, so the pair is bounded at ~2x this.
MAX_TRACE_FILE_BYTES = 2_000_000

#: How many finished records may wait for the writer before new ones are
#: dropped. Dropping a trace must never cost a turn, so the queue is small and
#: ``submit`` never blocks.
_WRITE_QUEUE_MAX = 64

#: How long an idle writer or finalizer thread waits for work before ending
#: itself. Long enough that a spoken turn's audio stages find the thread
#: started for it, short enough that idle threads never accumulate.
_WRITER_IDLE_EXIT_SEC = 2.0

#: A transcript older than this is not this turn's transcription end and is
#: discarded rather than mis-reported as a zero-length span.
_PENDING_TTL_SEC = 30.0

#: Default grace before an engine-only turn is written anyway. Voice turns are
#: written sooner, by the TTS first-byte hook or the listener's own fallback.
DEFAULT_FINISH_GRACE_SEC = 5.0

#: Environment override for the record file. Used by the test suite and by an
#: isolated instance so neither touches the owner's real file.
TRACE_PATH_ENV = "CLAIRE_TURN_TRACE_PATH"

#: Set truthy to switch tracing off entirely.
TRACE_DISABLED_ENV = "CLAIRE_TURN_TRACE_DISABLED"

_DEFAULT_TRACE_PATH = "~/.local/share/claire/turns.jsonl"


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def trace_path() -> Path:
    """The file records are appended to (``CLAIRE_TURN_TRACE_PATH`` wins)."""
    raw = os.environ.get(TRACE_PATH_ENV) or _DEFAULT_TRACE_PATH
    try:
        return Path(raw).expanduser()
    except Exception:  # pragma: no cover - defensive
        return Path(_DEFAULT_TRACE_PATH).expanduser()


def tracing_enabled() -> bool:
    """Whether new turns are traced at all."""
    return not _truthy(os.environ.get(TRACE_DISABLED_ENV))


def reasoning_text_of(response: Any) -> str:
    """The reasoning text a chat response carries, or ``""``.

    Handles both wire shapes the gateway can answer with — Ollama's top-level
    ``message`` and OpenAI's ``choices[0].message`` — and returns ``""`` for a
    response that carries none rather than inventing an empty string.
    """
    try:
        if not isinstance(response, dict):
            return ""
        message = response.get("message")
        if not isinstance(message, dict):
            choices = response.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
        if not isinstance(message, dict):
            return ""
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    except Exception:  # pragma: no cover - defensive
        return ""


@dataclass
class TurnTrace:
    """One turn's stages and the facts that make the numbers interpretable.

    ``mark`` records the *first* occurrence of a stage and ignores repeats:
    a stage that runs twice in a turn (the tool router can be re-run by
    ``toolSearchTool``) keeps the span of its first run, so the record stays a
    stable description of the turn. ``mark_latest`` is for stages whose last
    value is the meaningful one (``llm_last_token`` across a tool loop).
    """

    turn_id: str
    started_monotonic: float
    question_chars: int = 0
    answer_chars: int = 0
    question: Optional[str] = None
    answer: Optional[str] = None
    route_requested: Optional[str] = None
    route_served: Optional[str] = None
    #: Semantic-cache hit (work item 7.4). ``None`` means "the gateway did not
    #: say"; it is never set from a guess.
    cache_hit: Optional[bool] = None
    reasoning_present: bool = False
    reasoning_chars: int = 0
    marks: Dict[str, float] = field(default_factory=dict)
    finished: bool = False

    # -- stamping ----------------------------------------------------------

    def mark(self, name: str, at: Optional[float] = None) -> Optional[float]:
        """Stamp stage *name* at *at* (default: now). First stamp wins."""
        if name not in CANONICAL_STAGES:
            debug_log(f"turn trace: unknown stage {name!r} ignored", "trace")
            return None
        try:
            if name in self.marks:
                return self.marks[name]
            return self.mark_latest(name, at)
        except Exception:  # pragma: no cover - defensive
            return None

    def mark_latest(self, name: str, at: Optional[float] = None) -> Optional[float]:
        """Stamp stage *name*, overwriting any earlier stamp for it."""
        if name not in CANONICAL_STAGES:
            debug_log(f"turn trace: unknown stage {name!r} ignored", "trace")
            return None
        try:
            moment = float(at) if at is not None else time.monotonic()
            self.marks[name] = moment
            return moment
        except Exception:  # pragma: no cover - defensive
            return None

    def offset_of(self, name: str) -> Optional[float]:
        """Stage *name* as seconds since ``started_monotonic``, or ``None``."""
        moment = self.marks.get(name)
        if moment is None:
            return None
        return round(moment - self.started_monotonic, 6)

    # -- facts -------------------------------------------------------------

    def set_route(self, requested: Optional[str], served: Optional[str] = None) -> None:
        """Record the model asked for and the model that answered.

        *served* is left unset when the response did not name one: "the model
        that answered" is not something to guess from the model requested.
        """
        try:
            if requested is not None:
                self.route_requested = str(requested)
            if served is not None:
                self.route_served = str(served)
        except Exception:  # pragma: no cover - defensive
            pass

    def set_route_served(self, served: Optional[str]) -> None:
        self.set_route(None, served)

    def set_cache_hit(self, value: Optional[bool]) -> None:
        """Record a cache hit only when something actually observed one."""
        try:
            self.cache_hit = None if value is None else bool(value)
        except Exception:  # pragma: no cover - defensive
            pass

    def set_reasoning(self, text: Optional[str]) -> None:
        """Record whether the answer carried reasoning, and how many chars."""
        try:
            body = text or ""
            self.reasoning_present = bool(body)
            self.reasoning_chars = len(body)
        except Exception:  # pragma: no cover - defensive
            pass

    def set_question(self, text: Optional[str]) -> None:
        try:
            body = text or ""
            self.question_chars = len(body)
            if debug_enabled():
                self.question = body
        except Exception:  # pragma: no cover - defensive
            pass

    def set_answer(self, text: Optional[str]) -> None:
        try:
            body = text or ""
            self.answer_chars = len(body)
            if debug_enabled():
                self.answer = body
        except Exception:  # pragma: no cover - defensive
            pass

    # -- output ------------------------------------------------------------

    def last_mark(self) -> Optional[float]:
        return max(self.marks.values()) if self.marks else None

    def total_seconds(self) -> Optional[float]:
        """The turn's measured span: first stamp to last, or ``None``."""
        last = self.last_mark()
        if last is None:
            return None
        return round(last - self.started_monotonic, 6)

    def span(self, start: str, end: str) -> Optional[float]:
        """Seconds between two stamped stages, or ``None`` if either is absent."""
        first, second = self.marks.get(start), self.marks.get(end)
        if first is None or second is None:
            return None
        return round(second - first, 6)

    def llm_seconds(self) -> Optional[float]:
        """Generation time: from the end of planning to the last token.

        A turn whose planner was skipped has no ``planner_end``; the router's
        end is then the last event before generation began.
        """
        for start in ("planner_end", "router_end"):
            if start in self.marks and "llm_last_token" in self.marks:
                return self.span(start, "llm_last_token")
        return None

    def to_record(self) -> Dict[str, Any]:
        """The JSON object written for this turn. No text unless debug is on."""
        record: Dict[str, Any] = {
            "turn_id": self.turn_id,
            "logged_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "started_monotonic": round(self.started_monotonic, 6),
            "total_s": self.total_seconds(),
            "stages": {name: self.offset_of(name) for name in CANONICAL_STAGES
                       if name in self.marks},
            "durations": {
                "router_s": self.span("router_start", "router_end"),
                "planner_s": self.span("planner_start", "planner_end"),
                "llm_s": self.llm_seconds(),
                "speech_s": self.span("speech_start", "first_audio_byte"),
            },
            "route_requested": self.route_requested,
            "route_served": self.route_served,
            "cache_hit": self.cache_hit,
            "reasoning_present": self.reasoning_present,
            "reasoning_chars": self.reasoning_chars,
            "question_chars": self.question_chars,
            "answer_chars": self.answer_chars,
            "debug_text": bool(self.question is not None or self.answer is not None),
        }
        if self.question is not None:
            record["question"] = self.question
        if self.answer is not None:
            record["answer"] = self.answer
        return record

    def summary_line(self) -> str:
        """One journal line for the ``trace`` category. Never carries text."""
        def _s(value: Optional[float]) -> str:
            return "-" if value is None else f"{value:.2f}s"

        route = self.route_served or self.route_requested or "?"
        cache = {None: "unknown", True: "hit", False: "miss"}[self.cache_hit]
        return (
            f"turn {self.turn_id} total={_s(self.total_seconds())} "
            f"router={_s(self.span('router_start', 'router_end'))} "
            f"planner={_s(self.span('planner_start', 'planner_end'))} "
            f"llm={_s(self.llm_seconds())} "
            f"speech={_s(self.span('speech_start', 'first_audio_byte'))} "
            f"route={route} cache={cache} "
            f"reasoning={self.reasoning_chars}chars "
            f"q={self.question_chars}chars a={self.answer_chars}chars"
        )


# ---------------------------------------------------------------------------
# The bounded append-only writer
# ---------------------------------------------------------------------------

class _TraceWriter:
    """One background thread appending records; one rotation, two files max."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else trace_path()
        self._q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=_WRITE_QUEUE_MAX)
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self.dropped = 0
        self.written = 0

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._start_lock:
            self._start_thread_locked()

    def _start_thread_locked(self) -> None:
        """Start the writer if none is running. Caller holds ``_start_lock``."""
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._run, name="claire-turn-trace", daemon=True
        )
        self._thread = thread
        thread.start()

    def submit(self, record: Dict[str, Any]) -> None:
        """Queue one record. Never blocks and never raises."""
        if not tracing_enabled():
            return
        try:
            # The write, the idle check and the decision to end the thread all
            # hold ``_start_lock``, so a record cannot be queued just as the
            # writer decides it has nothing left to do.
            with self._start_lock:
                self._idle.clear()
                self._q.put_nowait(record)
                self._start_thread_locked()
        except queue.Full:
            self.dropped += 1
        except Exception:  # pragma: no cover - defensive
            self.dropped += 1

    def _run(self) -> None:  # pragma: no cover - thread body
        while True:
            try:
                record = self._q.get(timeout=_WRITER_IDLE_EXIT_SEC)
            except queue.Empty:
                # Nothing to write for a while: end the thread instead of
                # keeping one alive (and one per writer) for the life of the
                # process. A later submit starts a new one.
                with self._start_lock:
                    if not self._q.empty():
                        continue
                    self._idle.set()
                    if self._thread is threading.current_thread():
                        self._thread = None
                return
            except Exception:
                continue
            if record is None:
                # ``stop()``: everything queued before the sentinel is written.
                with self._start_lock:
                    if not self._q.empty():
                        continue
                    self._idle.set()
                    if self._thread is threading.current_thread():
                        self._thread = None
                return
            try:
                self._append(record)
            except Exception:
                pass
            finally:
                if self._q.empty():
                    self._idle.set()

    def stop(self, timeout: float = 2.0) -> None:
        """Drain what is queued and end the thread. Used between test runs."""
        with self._start_lock:
            thread, self._thread = self._thread, None
            if thread is None:
                return
            self._idle.clear()
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass  # the idle exit ends it once the queue drains
        thread.join(timeout=timeout)

    def _append(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self.written += 1
        try:
            if self.path.stat().st_size > MAX_TRACE_FILE_BYTES:
                self._rotate()
        except OSError:
            pass

    def _rotate(self) -> None:
        backup = self.path.with_name(self.path.name + ".1")
        try:
            os.replace(self.path, backup)
        except OSError:
            pass

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait for queued records to reach the file. For tests and readers."""
        if self._thread is None:
            return True
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._idle.is_set() and self._q.empty():
                return True
            time.sleep(0.005)
        return self._idle.is_set() and self._q.empty()


# ---------------------------------------------------------------------------
# The one-shot finalizer: no thread per turn
# ---------------------------------------------------------------------------

class _Finalizer:
    """Writes engine-only turns that no speaker ever finished.

    One daemon thread and a small heap, rather than a timer thread per turn:
    the gate drives thousands of turns and a thread each would be a leak with
    a schedule.
    """

    def __init__(self) -> None:
        self._heap: List[Tuple[float, int, TurnTrace]] = []
        self._cv = threading.Condition()
        self._seq = 0
        self._thread: Optional[threading.Thread] = None

    def _start_thread_locked(self) -> None:
        """Start the finisher if none is running. Caller holds ``_cv``."""
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._run, name="claire-turn-trace-finisher", daemon=True
        )
        self._thread = thread
        thread.start()

    def schedule(self, trace: TurnTrace, delay: float) -> None:
        try:
            with self._cv:
                # Replace any deadline already pending for this trace: the
                # listener extends the engine's safety window when it reaches
                # the speaker, and the earliest entry would otherwise win.
                self._heap = [item for item in self._heap if item[2] is not trace]
                self._seq += 1
                self._heap.append((time.monotonic() + max(0.0, delay), self._seq, trace))
                # Appending and starting the thread are one critical section, so
                # an entry cannot land just as the finisher decides to end.
                self._start_thread_locked()
                self._cv.notify_all()
        except Exception:  # pragma: no cover - defensive
            pass

    def stop(self, timeout: float = 2.0) -> None:
        """Drop pending deadlines and end the thread. Used between test runs."""
        with self._cv:
            thread, self._thread = self._thread, None
            self._heap.clear()
            self._cv.notify_all()
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self) -> None:  # pragma: no cover - thread body
        while True:
            trace: Optional[TurnTrace] = None
            try:
                with self._cv:
                    while True:
                        if not self._heap:
                            if self._cv.wait(timeout=_WRITER_IDLE_EXIT_SEC):
                                continue  # notified: look at the heap again
                            # Nothing scheduled for a while: end the thread
                            # instead of keeping one alive per finisher for the
                            # life of the process. A later schedule starts one.
                            if not self._heap:
                                if self._thread is threading.current_thread():
                                    self._thread = None
                                return
                            continue
                        self._heap.sort(key=lambda item: item[0])
                        deadline, _, candidate = self._heap[0]
                        remaining = deadline - time.monotonic()
                        if remaining > 0:
                            self._cv.wait(timeout=min(remaining, 60.0))
                            continue
                        self._heap.pop(0)
                        trace = candidate
                        break
            except Exception:
                continue
            if trace is not None and not trace.finished:
                _write_trace(trace)


# ---------------------------------------------------------------------------
# Module state and the public entry points
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_thread_local = threading.local()
_active: Optional[TurnTrace] = None
_pending_transcription_end: Optional[Tuple[float, float]] = None
_writer: Optional[_TraceWriter] = None
_finalizer: Optional[_Finalizer] = None


def _get_writer() -> _TraceWriter:
    global _writer
    with _lock:
        if _writer is None or _writer.path != trace_path():
            _writer = _TraceWriter()
        return _writer


def _get_finalizer() -> _Finalizer:
    global _finalizer
    with _lock:
        if _finalizer is None:
            _finalizer = _Finalizer()
        return _finalizer


def note_transcription_end(at: Optional[float] = None) -> float:
    """Record when this utterance's transcription finished.

    Called by the listener where the transcript is final. The value is
    consumed by the next :func:`begin_turn` on this thread, which becomes the
    record's zero point — so ``transcription_end`` is honest even though the
    trace itself is created later, by the reply engine.
    """
    global _pending_transcription_end
    moment = float(at) if at is not None else time.monotonic()
    try:
        with _lock:
            _pending_transcription_end = (moment, time.monotonic())
    except Exception:  # pragma: no cover - defensive
        pass
    return moment


def begin_turn(question: Optional[str] = None) -> TurnTrace:
    """Start (and register) the trace for a new turn.

    Always returns a fresh trace: turns are serialized by the query lock, and
    a previous turn whose record has not been written yet is closed by its own
    scheduled finish, not by this one.
    """
    global _active, _pending_transcription_end
    now = time.monotonic()
    started = now
    pending = None
    try:
        with _lock:
            pending = _pending_transcription_end
            _pending_transcription_end = None
    except Exception:  # pragma: no cover - defensive
        pending = None
    if pending is not None:
        moment, recorded_at = pending
        if recorded_at - now <= _PENDING_TTL_SEC and moment <= now:
            started = moment

    trace = TurnTrace(turn_id=uuid.uuid4().hex[:12], started_monotonic=started)
    if pending is not None and started == pending[0]:
        trace.mark("transcription_end", started)
    if question:
        trace.set_question(question)
    try:
        with _lock:
            _active = trace
        _thread_local.trace = trace
    except Exception:  # pragma: no cover - defensive
        pass
    return trace


def current_trace() -> Optional[TurnTrace]:
    """The trace for the turn running on this thread, else the active one."""
    trace = getattr(_thread_local, "trace", None)
    if trace is not None and not trace.finished:
        return trace
    with _lock:
        active = _active
    if active is not None and not active.finished:
        return active
    return None


def active_trace() -> Optional[TurnTrace]:
    """The process-wide active trace, for a stage stamped on another thread."""
    with _lock:
        return _active


def mark(name: str, at: Optional[float] = None) -> Optional[float]:
    """Stamp *name* on the current turn's trace. A no-op with no turn open."""
    trace = current_trace()
    if trace is None:
        return None
    return trace.mark(name, at)


def mark_latest(name: str, at: Optional[float] = None) -> Optional[float]:
    """Stamp *name* on the current turn, overwriting an earlier stamp."""
    trace = current_trace()
    if trace is None:
        return None
    return trace.mark_latest(name, at)


def mark_active(name: str, at: Optional[float] = None) -> Optional[float]:
    """Stamp the process-wide active trace from a worker thread.

    The TTS playback thread has no turn local of its own — it is spawned by the
    engine that owns the turn — so it reaches the turn through this instead.
    """
    trace = active_trace()
    if trace is None or trace.finished:
        return None
    return trace.mark(name, at)


def mark_reply_audio_start(at: Optional[float] = None) -> bool:
    """Stamp the reply's first device byte and write the turn's record.

    Called from the TTS playback thread. Returns ``False`` — and stamps
    nothing — until the listener has stamped ``speech_start`` for this turn,
    so an acknowledgement clip still draining from before the reply cannot be
    mistaken for the reply's audio. The record is written here because this is
    the last stage the contract names: once a byte has reached the device the
    turn's story is complete.
    """
    trace = active_trace()
    if trace is None or trace.finished:
        return False
    if "speech_start" not in trace.marks or "first_audio_byte" in trace.marks:
        return False
    if trace.mark("first_audio_byte", at) is None:
        return False
    _write_trace(trace)
    return True


def traced_stage(start_name: str, end_name: str):
    """Decorate a function so it stamps a stage pair around every return.

    ``try/finally`` rather than a stamp before each ``return``: a stage that
    runs is a stage that ends, including on the exception path, and the stamp
    lives next to the function it measures instead of at each caller.
    """
    def decorator(func):
        import functools

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            mark(start_name)
            try:
                return func(*args, **kwargs)
            finally:
                mark(end_name)

        return wrapper

    return decorator


def finish_turn(trace: Optional[TurnTrace] = None) -> None:
    """Write the record now and close the turn. Idempotent and non-blocking."""
    global _active
    if trace is None:
        trace = current_trace()
    if trace is None:
        return
    try:
        with _lock:
            if trace.finished:
                return
        _write_trace(trace)
    except Exception:  # pragma: no cover - defensive
        pass
    finally:
        try:
            with _lock:
                if _active is trace:
                    _active = None
                if getattr(_thread_local, "trace", None) is trace:
                    _thread_local.trace = None
        except Exception:  # pragma: no cover - defensive
            pass


def finish_turn_later(trace: Optional[TurnTrace] = None,
                      grace_sec: float = DEFAULT_FINISH_GRACE_SEC) -> None:
    """Schedule a write after *grace_sec*, unless something finishes sooner.

    The engine ends its part of the turn while the speaker may still be about
    to start, so it schedules rather than writes; the answer is on disk a
    couple of seconds later even if no speaker was ever reached.
    """
    if trace is None:
        trace = current_trace()
    if trace is None:
        return
    try:
        _get_finalizer().schedule(trace, grace_sec)
    except Exception:  # pragma: no cover - defensive
        pass


def _write_trace(trace: TurnTrace) -> None:
    """Serialise, hand to the writer and mirror the one-line summary."""
    with _lock:
        if trace.finished:
            return
        trace.finished = True
    try:
        record = trace.to_record()
    except Exception:  # pragma: no cover - defensive
        return
    try:
        _get_writer().submit(record)
    except Exception:  # pragma: no cover - defensive
        pass
    debug_log(trace.summary_line(), "trace")


def flush(timeout: float = 2.0) -> bool:
    """Wait for queued records to reach the file (tests and the reader)."""
    with _lock:
        writer = _writer
    if writer is None:
        return True
    return writer.flush(timeout)


def reset_for_tests() -> None:
    """Drop the in-process trace state so a test starts from nothing.

    The record is flushed and both helper threads are asked to end, so a suite
    that resets between tests does not leave a writer and a finisher behind for
    every test that drove a turn. The next turn starts fresh ones.
    """
    global _active, _pending_transcription_end, _writer, _finalizer
    with _lock:
        _active = None
        _pending_transcription_end = None
        writer, _writer = _writer, None
        finalizer, _finalizer = _finalizer, None
    try:
        if writer is not None:
            writer.flush(0.5)
            writer.stop()
        if finalizer is not None:
            finalizer.stop()
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        _thread_local.trace = None
    except Exception:  # pragma: no cover - defensive
        pass


# ---------------------------------------------------------------------------
# Reading records back: the CLI and the control socket share this
# ---------------------------------------------------------------------------

def _record_files(path: Optional[Path] = None) -> List[Path]:
    """The current file then its one backup, oldest first."""
    base = Path(path) if path is not None else trace_path()
    files = [base.with_name(base.name + ".1"), base]
    return [item for item in files if item.exists()]


def read_records(limit: Optional[int] = None, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The most recent records, oldest first.

    A malformed line is skipped rather than aborting the read: the file is
    written by a background thread and read by a human, and one bad line must
    not hide the rest.
    """
    records: List[Dict[str, Any]] = []
    for item in _record_files(path):
        try:
            with item.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        loaded = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(loaded, dict):
                        records.append(loaded)
        except OSError:
            continue
    if limit is not None and limit >= 0:
        return records[-limit:] if limit else []
    return records


def order_problems(record: Dict[str, Any]) -> List[str]:
    """Reasons the record's stages are missing or out of order.

    An empty list means every present stage is in the canonical order; a
    record with no stages at all is reported as missing the first stage rather
    than passing by vacuity.
    """
    stages = record.get("stages") if isinstance(record, dict) else None
    if not isinstance(stages, dict):
        return ["no stages recorded"]
    problems: List[str] = []
    present = [name for name in CANONICAL_STAGES if stages.get(name) is not None]
    for name in CANONICAL_STAGES:
        if stages.get(name) is None:
            problems.append(f"missing stage {name}")
    previous_name: Optional[str] = None
    previous_value: Optional[float] = None
    for name in present:
        value = stages.get(name)
        try:
            current = float(value)
        except (TypeError, ValueError):
            problems.append(f"stage {name} is not a number")
            continue
        if previous_value is not None and current < previous_value:
            problems.append(
                f"stage {name} ({current:.4f}) precedes {previous_name} "
                f"({previous_value:.4f})"
            )
        previous_name, previous_value = name, current
    return problems


def expected_stages_present(record: Dict[str, Any], *, voice: bool = True) -> List[str]:
    """The stages a complete turn should have that the record lacks.

    ``voice=False`` for a text turn: there is no transcription end to expect
    and no audio is spoken, so only the stages the reply engine itself stamps
    are required.
    """
    wanted: Iterable[str] = CANONICAL_STAGES if voice else _ENGINE_STAGES
    stages = record.get("stages") if isinstance(record, dict) else {}
    if not isinstance(stages, dict):
        stages = {}
    return [name for name in wanted if stages.get(name) is None]


def stage_span(record: Dict[str, Any], stage: str) -> Optional[float]:
    """The duration a stage contributed, for the CLI's ``--stage`` view."""
    stages = record.get("stages") if isinstance(record, dict) else None
    if not isinstance(stages, dict):
        return None
    try:
        if stage in ("router", "router_end"):
            return _delta(stages, "router_start", "router_end")
        if stage in ("planner", "planner_end"):
            return _delta(stages, "planner_start", "planner_end")
        if stage in ("llm", "llm_last_token"):
            if stages.get("planner_end") is not None:
                return _delta(stages, "planner_end", "llm_last_token")
            return _delta(stages, "router_end", "llm_last_token")
        if stage in ("speech", "first_audio_byte"):
            return _delta(stages, "speech_start", "first_audio_byte")
        if stage in ("transcription", "transcription_end", "stt"):
            return float(stages.get("transcription_end") or 0.0)
        value = stages.get(stage)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _delta(stages: Dict[str, Any], start: str, end: str) -> Optional[float]:
    first, second = stages.get(start), stages.get(end)
    if first is None or second is None:
        return None
    return round(float(second) - float(first), 6)


def format_record_line(record: Dict[str, Any]) -> str:
    """One readable line per record for the CLI."""
    stages = record.get("stages") if isinstance(record.get("stages"), dict) else {}
    total = record.get("total_s")
    total_text = "-" if total is None else f"{float(total):.2f}s"
    route = record.get("route_served") or record.get("route_requested") or "?"
    cache = record.get("cache_hit")
    cache_text = {None: "unknown", True: "hit", False: "miss"}.get(cache, "unknown")
    return (
        f"{str(record.get('logged_at') or '?')[:23]:<23} "
        f"total={total_text:<7} "
        f"router={_fmt_span(stage_span(record, 'router')):<7} "
        f"planner={_fmt_span(stage_span(record, 'planner')):<7} "
        f"llm={_fmt_span(stage_span(record, 'llm')):<7} "
        f"audio={_fmt_span(stage_span(record, 'first_audio_byte')):<7} "
        f"stages={len(stages)}/{len(CANONICAL_STAGES)} "
        f"route={route} cache={cache_text} "
        f"reasoning={int(record.get('reasoning_chars') or 0)}c "
        f"id={record.get('turn_id')}"
    )


def _fmt_span(value: Optional[float]) -> str:
    return "-" if value is None else f"{float(value):.2f}s"
