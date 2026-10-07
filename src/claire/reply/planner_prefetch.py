"""Speculative task-list planner, overlapped with the tool router.

The planner (``reply.planner.plan_query``) and the tool router
(``tools.selection.select_tools``) used to run strictly in series at the top of
the reply flow: the router first, so the planner could plan against the
*narrowed* catalogue it produced, and then the planner. Both are LLM
round-trips, and together they are the bulk of the pre-chat phase.

The planner only ever reads the redacted query and the dialogue window, both
known before the router runs. The narrowing was a *quality* device, not a
dependency: it named the likely tools so a small planner would not paraphrase
or reach for ``webSearch``. So the router's round-trip was pure serial
latency in front of a call that did not need it.

This module runs the planner on a background thread the moment the query and
dialogue window are known, planning against the **full** catalogue because the
router's narrowing does not exist yet. Callers wait for it only when the
router's output says the plan will actually be used — the engine's fast path
still discards the plan (and never waits) for a short, tool-free turn.

Measured on this host (paired adjacent arms, every (query, arm) a distinct
prompt so the gateway cache cannot serve one arm from the other):

- Planning against the full catalogue costs the planner nothing measurable.
  26 varied queries, the 23 that actually reach the planner: paired median
  delta +0.03 s, mean -0.06 s, and the direction is two-sided — 4 queries
  graded worse on the full catalogue, 3 worse on the narrowed one. The earlier
  "2-3x slower" estimate came from the raw ``generate_tools_json_schema``
  character count, but the planner is handed names plus one truncated
  description line, so 15 tools is ~1.7k characters, not 10.7k.
- End-to-end pre-chat (time to the first chat call, 6 paired repeats per arm):
  -1.01 s on a plain tool turn, -1.85 s on a meals turn, -1.55 s on a memory
  turn, and no regression on turns whose plan is discarded (-0.38 s / +0.14 s
  on two short tool-free turns) — the router is not slowed by the extra
  in-flight call.

Waiting is bounded by the budget the serial ordering would have given the
planner, so the overlap cannot make a turn slower; see the engine's
consumption site for the three invariants and `tests/test_planner_prefetch.py`
for the tests that pin them.

Failure is always non-fatal: an error or a non-list result yields ``None`` and
the engine falls back to the serial, router-narrowed planner. A legitimate
*empty* plan (``[]``) is returned as-is — it is the planner's positive
"nothing to do" answer, not a failure.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from ..debug import debug_log


class PlannerPrefetch:
    """A single in-flight speculative planner call.

    ``result()`` waits at most the remainder of the call's own budget, so a
    speculative planner can never make a turn slower than the serial planner
    it replaces: the worker's deadline starts when the thread starts, before
    the router, so the wait after the router is the budget minus however long
    the router took.
    """

    def __init__(
        self,
        *,
        call: Callable[[], Optional[List[str]]],
        timeout_sec: float,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self._call = call
        self._timeout_sec = float(timeout_sec)
        self._on_error = on_error
        self._started_at = time.monotonic()
        self._done = threading.Event()
        self._plan: Optional[List[str]] = None
        self._thread = threading.Thread(
            target=self._run,
            name="planner-prefetch",
            daemon=True,
        )

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> "PlannerPrefetch":
        """Begin the speculative call. Never raises."""
        try:
            self._thread.start()
        except Exception as exc:  # pragma: no cover - Thread.start can fail on
            # resource exhaustion; treat it exactly like a failed plan.
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
        # A plan is a list of step strings. Anything else (None from a failed
        # backend call, a stray scalar) is "no plan".
        self._plan = list(value) if isinstance(value, list) else None
        self._done.set()

    def _report_error(self, exc: BaseException) -> None:
        try:
            if self._on_error is not None:
                self._on_error(exc)
            else:
                debug_log(f"planner prefetch failed (non-fatal): {exc}", "planning")
        except Exception:  # noqa: BLE001 - reporting must never raise either.
            pass

    # ── consumption ──────────────────────────────────────────────────────

    @property
    def timeout_sec(self) -> float:
        return self._timeout_sec

    @property
    def settled(self) -> bool:
        """True once the worker has finished, whether it planned or failed.

        This is what lets a caller tell a *timeout* (worker still in flight:
        the backend has already been given the full planner ceiling and did
        not answer, so a serial retry can only fail the same way) from a
        *settled failure* (the call raised or returned nothing immediately,
        so a serial retry is cheap and may still produce a plan).
        """
        return self._done.is_set()

    def wait(self, within: float) -> Optional[List[str]]:
        """Wait up to ``within`` seconds **from now** for the plan.

        :meth:`result` bounds its wait from :meth:`start`, which is what a
        caller needs to cap the whole speculative window. This method
        instead lets a caller spend the *router's* duration on top of the
        planner's own ceiling — i.e. hand the planner exactly the wall-clock
        the serial ordering would have given it (router, then a full
        planner timeout) without giving up the overlap.

        Returns ``None`` when the worker has not settled, and also when it
        settled without a usable plan; check :attr:`settled` to tell those
        apart. Never raises.
        """
        if within > 0:
            self._done.wait(within)
        if not self._done.is_set():
            # Still in flight: abandon it. The thread is a daemon, holds no
            # locks, and nothing it publishes is read.
            return None
        return self._plan

    def result(self) -> Optional[List[str]]:
        """Return the plan, or ``None`` if it is not available in budget.

        Never raises, and never blocks longer than ``timeout_sec`` measured
        from :meth:`start`.
        """
        remaining = self._timeout_sec - (time.monotonic() - self._started_at)
        if remaining > 0:
            self._done.wait(remaining)
        if not self._done.is_set():
            # The worker is still in flight. Abandon it — it is a daemon
            # thread, it holds no locks, and nothing it publishes is read.
            return None
        return self._plan


def start_planner_prefetch(
    *,
    call: Callable[[], Optional[List[str]]],
    timeout_sec: float,
) -> PlannerPrefetch:
    """Start a speculative planner call and return its handle.

    The callable is invoked on a background thread. ``call`` is expected to
    be already bound (closure or ``functools.partial``) so that a test which
    patches the planner symbol still captures the background call.
    """
    return PlannerPrefetch(call=call, timeout_sec=timeout_sec).start()
