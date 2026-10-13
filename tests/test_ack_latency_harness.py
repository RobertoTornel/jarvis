"""Unit tests for ``scripts/measure_ack_latency.py`` (the clip latency harness).

These test the *harness's own logic*, never a real audio device: the capture,
the clock and the players are fakes, so nothing here opens a stream, plays a
clip or reaches the network.

The properties the task requires are covered explicitly:

* a path that was not measured is reported as ``not measured`` and exits
  non-zero, so no number is ever fabricated,
* the clip path beating or missing the target decides the exit code,
* a trial whose gap was not quiet is rejected rather than averaged in,
* a player that refuses, or a capture that never hears anything, is an error
  and not a silent zero.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "measure_ack_latency.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("measure_ack_latency", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["measure_ack_latency"] = module
    spec.loader.exec_module(module)
    return module


h = _load_harness()

pytestmark = pytest.mark.unit


class FakeCapture:
    """A capture whose armed window yields whatever the test says it hears."""

    def __init__(self, *, gap_peak: float = 0.0, sound_at=0.05, window_peak: float = 0.02):
        self.gap_peak = gap_peak
        self.sound_at = sound_at
        self.window_peak = window_peak
        self.armed_threshold = None
        self.armed = 0
        self.first = None

    def disarm(self) -> None:
        self.first = None
        self.armed_threshold = None

    def drain(self, seconds: float) -> float:
        return self.gap_peak

    def arm(self, threshold=None) -> float:
        self.armed += 1
        self.armed_threshold = threshold
        self.first = self.sound_at
        return 0.0

    def first_sound(self):
        return self.first


class FakePlayer:
    def __init__(self, *, accepts: bool = True, speaking: bool = False):
        self.accepts = accepts
        self._speaking = speaking
        self.played = 0

    def play(self, clip) -> bool:
        if not self.accepts:
            return False
        self.played += 1
        return True

    def is_speaking(self) -> bool:
        return self._speaking

    def stop(self) -> None:
        self._speaking = False


# ---------------------------------------------------------------------------
# Percentiles and result summarising
# ---------------------------------------------------------------------------

def test_percentile_matches_numpy():
    values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
    for percent in (0, 25, 50, 75, 95, 100):
        assert h.percentile(values, percent) == pytest.approx(
            np.percentile(values, percent)
        )


def test_summarise_leaves_an_empty_result_unmeasured():
    result = h.PathResult("clip")
    result.summarise()
    assert result.p50_sec is None and result.p95_sec is None


def test_summarise_reports_p50_and_p95():
    result = h.PathResult("clip", samples_sec=[0.1, 0.2, 0.3, 0.4, 0.5])
    result.summarise()
    assert result.p50_sec == pytest.approx(0.3)
    assert result.p95_sec == pytest.approx(0.48)


# ---------------------------------------------------------------------------
# Detection responds to the gap, a refusal, and silence
# ---------------------------------------------------------------------------

def test_measure_clip_path_rejects_a_noisy_gap(monkeypatch):
    capture = FakeCapture(gap_peak=0.5)
    result = h.measure_clip_path(
        FakePlayer(), object(), capture, trials=3, gap_sec=0.01, timeout=0.05,
        threshold=0.01,
    )
    assert result.trials_rejected == 3
    assert result.samples_sec == []
    assert result.trials_accepted == 0


def test_measure_clip_path_records_the_first_sound(monkeypatch):
    capture = FakeCapture(gap_peak=0.0, sound_at=0.123, window_peak=0.03)
    result = h.measure_clip_path(
        FakePlayer(), object(), capture, trials=2, gap_sec=0.01, timeout=0.05,
        threshold=0.01,
    )
    assert result.samples_sec == [pytest.approx(0.123)] * 2
    assert result.trials_accepted == 2
    assert result.monitor_peak == pytest.approx(0.03)


def test_measure_clip_path_refuses_to_report_a_silent_player():
    capture = FakeCapture(gap_peak=0.0)
    capture.arm = lambda threshold=None: setattr(capture, "first", None) or 0.0
    with pytest.raises(RuntimeError):
        h.measure_clip_path(
            FakePlayer(), object(), capture, trials=1, gap_sec=0.01, timeout=0.05,
            threshold=0.01,
        )


def test_measure_clip_path_treats_a_refused_play_as_an_error():
    capture = FakeCapture(gap_peak=0.0)
    with pytest.raises(RuntimeError):
        h.measure_clip_path(
            FakePlayer(accepts=False), object(), capture, trials=1, gap_sec=0.01,
            timeout=0.05, threshold=0.01,
        )


# ---------------------------------------------------------------------------
# Reporting never invents a number
# ---------------------------------------------------------------------------

def _result(**paths):
    base = {
        "control": None,
        "capture_device": "fake.monitor",
        "platform": "test",
        "paths": {
            "clip": h.asdict(h.PathResult("clip")),
            "synthesis": h.asdict(h.PathResult("synthesis")),
        },
    }
    base["paths"].update(paths)
    return base


def test_report_says_not_measured_without_a_clip_number():
    exit_code = h.report(_result())
    assert exit_code == h.EXIT_NOT_MEASURED


def test_report_prefers_no_claim_when_the_clip_trial_was_rejected():
    clip = h.PathResult("clip", trials_requested=5, trials_rejected=5)
    exit_code = h.report(_result(clip=h.asdict(clip)))
    assert exit_code == h.EXIT_NOT_MEASURED


def test_report_accepts_a_clip_path_inside_the_target():
    clip = h.PathResult("clip", samples_sec=[0.1, 0.12, 0.11], status=h.STATUS_MEASURED)
    clip.summarise()
    synth = h.PathResult("synthesis", samples_sec=[0.7, 0.9], status=h.STATUS_MEASURED)
    synth.summarise()
    assert h.report(_result(clip=h.asdict(clip), synthesis=h.asdict(synth))) == h.EXIT_OK


def test_report_flags_a_clip_path_that_does_not_beat_synthesis():
    clip = h.PathResult("clip", samples_sec=[0.3, 0.35], status=h.STATUS_MEASURED)
    clip.summarise()
    synth = h.PathResult("synthesis", samples_sec=[0.2, 0.21], status=h.STATUS_MEASURED)
    synth.summarise()
    assert (
        h.report(_result(clip=h.asdict(clip), synthesis=h.asdict(synth)))
        == h.EXIT_TARGET_MISSED
    )


def test_report_is_not_measured_without_a_monitor_device():
    section = h.PathResult("clip")
    section.reason = "no sink monitor source was found to capture"
    assert (
        h.report({"capture_device": None, "platform": "test",
                  "paths": {"clip": h.asdict(section),
                            "synthesis": h.asdict(h.PathResult("synthesis"))}})
        == h.EXIT_NOT_MEASURED
    )


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

def test_monitor_for_sink_finds_the_monitor(monkeypatch):
    listing = "59\talsa_output.usb.monitor\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
    monkeypatch.setattr(
        h.subprocess, "run",
        lambda *a, **k: SimpleNamespace(stdout=listing, returncode=0),
    )
    assert h.monitor_for_sink("alsa_output.usb") == "alsa_output.usb.monitor"


def test_monitor_for_sink_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(
        h.subprocess, "run",
        lambda *a, **k: SimpleNamespace(stdout="59\tother\tPipeWire\tx\tRUNNING\n", returncode=0),
    )
    assert h.monitor_for_sink("alsa_output.usb") is None
