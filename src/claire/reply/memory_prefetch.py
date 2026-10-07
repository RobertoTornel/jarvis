"""Speculative keyword-memory-extractor prefetch (Finding E).

The keyword memory extractor (``reply.enrichment
.extract_search_params_for_memory``) depends on exactly two inputs: the
redacted user query and the compact enrichment context hint. Both are known
*before* the tool router and the planner run. Those two hops exist only to
decide *whether* the turn needs long-term memory, so the extractor currently
sits serially behind a decision it does not depend on.

This module runs the extractor on a background thread so that decision is
paid for while the extractor is already in flight, and publishes the answer
into the same conversation-scoped hot-window cache entry the serial path
would have written. When the planner later asks for memory *without* a topic
hint, the engine consults the cache, finds the answer already there, and
skips the LLM round-trip entirely.

Scope is deliberately narrow, because the extractor's answer is only
reusable when its input is *byte-identical* to the serial call's:

* Only the no-topic-hint query is prefetched. A planner that emits
  ``searchMemory topic='...'`` appends ``"\\n[Memory topic: ...]"`` to the
  extractor's user content, which measurably changes the extracted
  keywords/questions (and is sometimes the difference between a usable
  answer and an empty one). That answer is therefore **never** substituted
  for a topic-hinted request; it is simply left in the cache under the
  no-hint key.
* The result is published only while the hot-cache generation is unchanged.
  An abandoned prefetch can never leak into a later conversation.

Failure is always non-fatal: any error in the background call is swallowed
and reported as ``None``, and the engine falls back to the ordinary serial
call.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from ..debug import debug_log


class MemoryExtractorPrefetch:
    """A single in-flight speculative extractor call.

    Construct with the zero-argument ``call`` that performs the real
    extraction (the engine passes a closure over its module-level
    ``extract_search_params_for_memory`` so tests that patch that symbol keep
    intercepting it), then :meth:`start` it. The caller is responsible for
    calling :meth:`result` when it wants the value.

    The object is safe to abandon: the worker is a daemon thread, it catches
    its own exceptions, and publishing is guarded by the cache generation it
    captured at construction.
    """

    def __init__(
        self,
        *,
        cache_key: str,
        call: Callable[[], Any],
        dialogue_memory: Any,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self._cache_key = cache_key
        self._call = call
        self._dm = dialogue_memory
        self._on_error = on_error
        self._started_at = time.monotonic()
        self._done = threading.Event()
        self._result: Optional[dict] = None
        # Captured now, before the thread can publish: if the cache is wiped
        # while this call is in flight, the epoch moves and the publish is
        # dropped rather than landing in a different conversation.
        self._epoch = self._read_epoch()
        self._thread = threading.Thread(
            target=self._run,
            name="memory-extractor-prefetch",
            daemon=True,
        )

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> "MemoryExtractorPrefetch":
        """Begin the speculative call. Never raises."""
        try:
            self._thread.start()
        except Exception as exc:  # pragma: no cover - Thread.start can fail on
            # resource exhaustion; treat it exactly like a failed extraction.
            self._done.set()
            self._report_error(exc)
        return self

    def _run(self) -> None:
        try:
            value = self._call()
        except BaseException as exc:  # noqa: BLE001 - must never escape the
            # thread into the interpreter's unhandled-exception hook.
            value = None
            self._report_error(exc)
        if isinstance(value, dict):
            self._result = value
            self._publish(value)
        self._done.set()

    def _publish(self, value: dict) -> None:
        if self._epoch != self._read_epoch():
            debug_log(
                "memory extractor prefetch dropped: hot cache was reset",
                "memory",
            )
            return
        put = getattr(self._dm, "hot_cache_put", None)
        if not callable(put):
            return
        try:
            put(self._cache_key, value)
            debug_log(
                "memory extractor prefetch published to hot-window cache",
                "memory",
            )
        except Exception as exc:  # noqa: BLE001 - a cache write must never
            # take down the reply.
            self._report_error(exc)

    # ── reading the value ────────────────────────────────────────────────

    def result(self, budget_sec: float) -> Optional[dict]:
        """Return the prefetched dict, or ``None`` if it is not available.

        ``budget_sec`` is the *whole* ceiling the caller would have given the
        serial extractor call. The wait is clamped to whatever remains of it
        after this prefetch's own elapsed time, so the prefetch can never
        hold a turn longer than the plain call it replaced: the worker's own
        backend call is ceilinged at that same budget and therefore settles
        within it.

        Returns ``None`` for a non-dict response, a failed call, or a wait
        that expired — the caller then falls back to the ordinary serial
        call.
        """
        remaining = float(budget_sec) - (time.monotonic() - self._started_at)
        if remaining > 0:
            self._done.wait(remaining)
        if not self._done.is_set() or self._result is None:
            return None
        return self._result

    @property
    def elapsed(self) -> float:
        """Seconds since :meth:`start` (or construction) was reached."""
        return time.monotonic() - self._started_at

    @property
    def done(self) -> bool:
        return self._done.is_set()

    # ── internals ────────────────────────────────────────────────────────

    def _read_epoch(self) -> Optional[int]:
        try:
            return int(self._dm.hot_cache_epoch)
        except Exception:  # noqa: BLE001 - a cache without a generation is
            # treated as always-current rather than never-publishable, which
            # keeps custom test doubles working.
            return None

    def _report_error(self, exc: BaseException) -> None:
        if self._on_error is not None:
            try:
                self._on_error(exc)
                return
            except Exception:  # noqa: BLE001
                pass
        debug_log(f"memory extractor prefetch failed: {exc}", "memory")


def start_memory_extractor_prefetch(
    *,
    query: str,
    call: Callable[[], Any],
    dialogue_memory: Any,
) -> Optional[MemoryExtractorPrefetch]:
    """Start a speculative extractor call for ``query``.

    Returns ``None`` when there is nowhere to publish the result — without a
    usable hot cache the prefetch could only be consumed by blocking on it,
    which is exactly the serial call the engine already makes. Also returns
    ``None`` when the no-hint answer is already cached, since a second
    speculative call would be pure waste.
    """
    if dialogue_memory is None:
        return None
    get = getattr(dialogue_memory, "hot_cache_get", None)
    put = getattr(dialogue_memory, "hot_cache_put", None)
    if not callable(get) or not callable(put):
        return None

    cache_key = f"enrichment:{query}"
    try:
        if isinstance(get(cache_key), dict):
            debug_log(
                "memory extractor prefetch skipped: already in hot-window cache",
                "memory",
            )
            return None
    except Exception:  # noqa: BLE001 - an unreadable cache is not fatal.
        pass

    prefetch = MemoryExtractorPrefetch(
        cache_key=cache_key,
        call=call,
        dialogue_memory=dialogue_memory,
    )
    return prefetch.start()
