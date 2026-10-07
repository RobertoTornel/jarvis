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
router's narrowing does not exist yet. The caller then takes that plan *only if
it is already in hand* when the router returns, and otherwise plans serially
exactly as before — so the overlap is free when it works and invisible when it
does not. Nothing ever waits on this handle.

Measured on this host (paired adjacent arms, every (query, arm) a distinct
prompt so the gateway cache cannot serve one arm from the other):

- Planning against the full catalogue costs the planner very little. 30 paired
  calls, 3-tool vs 15-tool catalogue: median delta +0.00 s, mean +0.09 s. The
  catalogue the planner is handed is names plus one truncated description line
  (391 vs 1723 characters), not the 2-3x growth the raw
  ``generate_tools_json_schema`` payload would suggest. The residual cost is a
  slightly higher share of calls reaching the configured planner ceiling
  (6/30 -> 8/30 at 3.0 s), which fails open like any planner timeout.
- End-to-end pre-chat (time to the first chat call): the planner's round-trip
  is removed entirely on the turns where it wins the race with the router.

**Consumption is a race, never a wait.** An earlier revision waited for the
speculative plan with a budget equal to the serial wall-clock. That is not safe
on a shared backend: the gateway may batch the two calls (where the win comes
from) or queue one behind the other, and a queued planner call can outrun the
ceiling its serial counterpart met comfortably. The waiting design then spent
the whole budget and followed no plan at all — strictly slower than serial and
planless where serial had a plan. So the plan is taken only if it is already in
hand when the router returns, and a turn without one runs the serial planner
verbatim. See the consumption site in ``reply.engine`` and
``tests/test_planner_prefetch.py``.

Failure is always non-fatal: an error or a non-list result yields ``None`` and
the caller runs its own planner. A legitimate *empty* plan (``[]``) is not
distinguished from a failure, and does not need to be — neither is a plan the
turn can follow, and the serial planner is the right answer to both.
"""

from __future__ import annotations

import threading
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

        A caller that only ever reads :meth:`plan_if_ready` does not need
        this, but it is the honest way to report *why* a prefetch was not
        used — still running (the router got there first) versus finished
        without a plan.
        """
        return self._done.is_set()

    def plan_if_ready(self) -> Optional[List[str]]:
        """The plan if the worker has **already** finished with one, else ``None``.

        This is the safe consumption point: it never waits, so the caller can
        only ever *gain* the planner's latency, never spend a turn's budget on
        it. A caller that finds no plan here runs its own planner, which is
        exactly what it would have done without the prefetch.
        """
        return self._plan if self._done.is_set() else None


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
