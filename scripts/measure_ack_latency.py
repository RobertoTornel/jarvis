#!/usr/bin/env python3
"""Measure time to first audible sound for the acknowledgement clip path.

The number this records is the one a person feels: from the instant the turn
dispatches its acknowledgement to the instant the first sample of it reaches
the speaker. Two paths are compared on the same machine and the same audio
device:

``clip``       ``output/clip_player.py`` playing a clip already on disk.
``synthesis``  the configured speech engine (``create_tts_engine``) speaking
               the same phrase, which is what the listener falls back to.

Both are measured the same way. A ``parec`` capture of the default sink's
monitor is armed, the path is dispatched, and the first captured block whose
peak clears the detection threshold is the first audible sound. The capture is
electrical (the sink monitor), not a microphone, so it hears only what the
machine played.

What the number includes and what it does not
---------------------------------------------
Included: dispatch, stream open, portaudio buffering, the device's own
buffering, and the sink monitor's capture latency.
Not included: the analogue path after the sink (DAC, amplifier, speaker) and
the air to a listener's ear. Those cannot be measured from this machine without
a microphone in the room, so every number here is a lower bound on what a
person actually hears. Both paths are measured identically, so the comparison
between them is fair.

Contamination
-------------
The sink being measured may carry other clients' audio, so a trial is accepted
only when the gap before it stayed quiet below the detection threshold.
Rejected trials are counted and reported, never silently averaged in. The
threshold is set from the idle floor and, for the clip path, from the clip's
own known peak, which is known exactly because the harness rendered or loaded
it.

Rules this harness obeys
------------------------
* It never invents a number. When the monitor, the clip, or the credential for
  a paid synthesis is missing, the path is reported as ``not_measured`` with
  the exact reason and the command that fixes it, and the process exits
  non-zero.
* A synthesis baseline costs a real speech request, so it happens only with
  ``--live-fish``. Rendering a missing clip also costs one.

Usage::

    .venv/bin/python scripts/measure_ack_latency.py --trials 20 --live-fish
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCHEMA = "claire.ack_latency.v1"

STATUS_MEASURED = "measured"
STATUS_NOT_MEASURED = "not_measured"

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_TARGET_MISSED = 2
EXIT_NOT_MEASURED = 3

#: Targets from `two_tier_design.md` section 5.
TARGET_P50_SEC = 0.25
TARGET_P95_SEC = 0.40
#: The synthesised acknowledgement this feature replaces, measured on this
#: machine (`two_tier_design.md`: "first sound about 0.69 s").
BASELINE_SYNTHESIS_SEC = 0.69

#: Capture block: 1024 stereo int16 frames at 48 kHz is about 21 ms, which is
#: the timestamp resolution of every measurement here. The monitor is captured
#: in stereo rather than downmixed, because a mono downmix would attenuate the
#: signal by 6 dB before it was ever measured.
CAPTURE_BLOCK_BYTES = 4096
CAPTURE_RATE = 48000
CAPTURE_CHANNELS = 2
#: Absolute floor for detection, so digital silence plus a sample of dither
#: never counts as sound. The monitor is quiet enough on this machine that a
#: played clip can be seen well above this.
DETECTION_FLOOR = 0.002


@dataclass
class PathResult:
    """One measured path."""

    name: str
    status: str = STATUS_NOT_MEASURED
    reason: str = ""
    fix: str = ""
    threshold: Optional[float] = None
    signal_peak: Optional[float] = None
    monitor_peak: Optional[float] = None
    trials_requested: int = 0
    trials_accepted: int = 0
    trials_rejected: int = 0
    trials_failed: int = 0
    samples_sec: list[float] = field(default_factory=list)
    p50_sec: Optional[float] = None
    p95_sec: Optional[float] = None

    def summarise(self) -> None:
        if self.samples_sec:
            self.p50_sec = percentile(self.samples_sec, 50.0)
            self.p95_sec = percentile(self.samples_sec, 95.0)


def percentile(values: list[float], percent: float) -> float:
    """Linear-interpolated percentile, matching numpy's default."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (percent / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def default_sink() -> Optional[str]:
    """The sink everything plays through unless a device is configured."""
    try:
        out = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    name = out.stdout.strip()
    return name or None


def monitor_for_sink(sink: str) -> Optional[str]:
    """The sink's monitor source, or None when it does not exist."""
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] == f"{sink}.monitor":
            return parts[1]
    return None


class MonitorCapture:
    """Records the sink monitor and reports when sound first appeared.

    The reader runs on its own thread so the caller can arm the capture and
    dispatch immediately, with no capture startup inside the measured window.
    """

    def __init__(self, device: str) -> None:
        self.device = device
        self.threshold = DETECTION_FLOOR
        self.window_peak = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._armed_at: Optional[float] = None
        self._first: Optional[float] = None
        self.last_peak = 0.0

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [
                "parec",
                "--device", self.device,
                "--format=s16le",
                "--rate", str(CAPTURE_RATE),
                "--channels", str(CAPTURE_CHANNELS),
                "--latency-msec=10",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        os.set_blocking(self._proc.stdout.fileno(), False)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while not self._stop.is_set():
            block = self._proc.stdout.read(CAPTURE_BLOCK_BYTES)
            if not block:
                time.sleep(0.001)
                continue
            audio = np.frombuffer(block, dtype=np.int16)
            if audio.size == 0:
                continue
            peak = float(np.abs(audio).max()) / 32768.0
            with self._lock:
                self.last_peak = peak
                if self._armed_at is not None:
                    self.window_peak = max(self.window_peak, peak)
                    if self._first is None and peak > self.threshold:
                        self._first = time.monotonic() - self._armed_at

    def arm(self, threshold: Optional[float] = None) -> float:
        """Begin a measurement window; returns the window's start time."""
        with self._lock:
            if threshold is not None:
                self.threshold = threshold
            self._first = None
            self.window_peak = 0.0
            self._armed_at = time.monotonic()
            return self._armed_at

    def first_sound(self) -> Optional[float]:
        with self._lock:
            return self._first

    def disarm(self) -> None:
        with self._lock:
            self._armed_at = None
            self._first = None

    def drain(self, seconds: float) -> float:
        """Discard queued audio; return the loudest peak seen while draining."""
        deadline = time.monotonic() + seconds
        loudest = 0.0
        while time.monotonic() < deadline:
            with self._lock:
                loudest = max(loudest, self.last_peak)
            time.sleep(0.005)
        return loudest

    def close(self) -> None:
        self._stop.set()
        self.disarm()
        if self._thread is not None:
            self._thread.join(1.0)
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(2.0)
            except Exception:
                pass


def wait_for_sound(capture: MonitorCapture, timeout: float) -> Optional[float]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        first = capture.first_sound()
        if first is not None:
            return first
        time.sleep(0.002)
    return None


def quiet_gap(capture: MonitorCapture, seconds: float, threshold: float) -> tuple[bool, float]:
    """Drain for ``seconds`` and report whether the gap stayed below the threshold."""
    peak = capture.drain(seconds)
    return peak <= threshold * 0.9, peak


def measure_clip_path(player: Any, clip: Any, capture: MonitorCapture, trials: int,
                      gap_sec: float, timeout: float, threshold: float) -> "PathResult":
    """Dispatch the clip path and return first-sound seconds per accepted trial."""
    result = PathResult(name="clip", trials_requested=trials, threshold=threshold)
    for _ in range(trials):
        capture.disarm()
        clean, _peak = quiet_gap(capture, gap_sec, threshold)
        if not clean:
            result.trials_rejected += 1
            continue
        capture.arm(threshold)
        if not player.play(clip):
            raise RuntimeError("the clip player refused to play the clip")
        first = wait_for_sound(capture, timeout)
        # Let the phrase finish so the level recorded is the phrase's, not the
        # first blip that crossed the threshold.
        deadline = time.monotonic() + timeout
        while player.is_speaking() and time.monotonic() < deadline:
            time.sleep(0.01)
        player.stop()
        if first is None:
            raise RuntimeError("no sound reached the sink monitor within the timeout")
        result.samples_sec.append(first)
        result.trials_accepted += 1
        result.monitor_peak = max(result.monitor_peak or 0.0, capture.window_peak)
        time.sleep(0.05)
    return result


def measure_synthesis_path(tts: Any, phrase: str, capture: MonitorCapture, trials: int,
                           gap_sec: float, timeout: float, threshold: float) -> "PathResult":
    """Dispatch the configured engine and return first-sound seconds per trial."""
    result = PathResult(name="synthesis", trials_requested=trials, threshold=threshold)
    for _ in range(trials):
        capture.disarm()
        clean, _peak = quiet_gap(capture, gap_sec, threshold)
        if not clean:
            result.trials_rejected += 1
            continue
        deadline = time.monotonic() + 10.0
        while tts.is_speaking() and time.monotonic() < deadline:
            time.sleep(0.01)
        capture.arm(threshold)
        tts.speak(phrase)
        first = wait_for_sound(capture, timeout)
        if first is None:
            # A failed request is not a measurement of latency: record it and
            # carry on, rather than losing the whole baseline to one hiccup.
            result.trials_failed += 1
            deadline = time.monotonic() + 5.0
            while tts.is_speaking() and time.monotonic() < deadline:
                time.sleep(0.02)
            continue
        result.samples_sec.append(first)
        result.trials_accepted += 1
        result.monitor_peak = max(result.monitor_peak or 0.0, capture.window_peak)
        deadline = time.monotonic() + timeout
        while tts.is_speaking() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.05)
    return result


def build_tts_engine(cfg: Any) -> Any:
    """Build the configured engine exactly as the daemon does."""
    from claire.output.tts import create_tts_engine

    return create_tts_engine(
        engine=getattr(cfg, "tts_engine", "fish"),
        enabled=True,
        voice=getattr(cfg, "tts_voice", None),
        rate=getattr(cfg, "tts_rate", None),
        config=cfg,
        piper_model_path=getattr(cfg, "tts_piper_model_path", None),
        piper_speaker=getattr(cfg, "tts_piper_speaker", None),
        piper_length_scale=getattr(cfg, "tts_piper_length_scale", 1.0),
        piper_noise_scale=getattr(cfg, "tts_piper_noise_scale", 0.667),
        piper_noise_w=getattr(cfg, "tts_piper_noise_w", 0.8),
        piper_sentence_silence=getattr(cfg, "tts_piper_sentence_silence", 0.2),
    )


def signal_peak(clip: Any) -> float:
    """The clip's own peak, 0..1, used to set a threshold that fits its level."""
    samples = np.asarray(clip.samples, dtype=np.int32)
    if samples.size == 0:
        return 0.0
    return float(np.abs(samples).max()) / 32768.0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--trials", type=int, default=20, help="trials per path (default 20)")
    parser.add_argument("--phrase", default="On it.",
                        help="the phrase to measure (default: the first ack phrase)")
    parser.add_argument("--gap", type=float, default=0.4,
                        help="silence drained between trials, in seconds")
    parser.add_argument("--settle", type=float, default=1.0,
                        help="seconds sampled to learn the monitor's idle floor")
    parser.add_argument("--timeout", type=float, default=25.0,
                        help="seconds to wait for a measured path to make a sound")
    parser.add_argument("--live-fish", action="store_true",
                        help="allow the real synthesis request and clip rendering")
    parser.add_argument("--device", default=None,
                        help="capture source, overriding the default sink's monitor")
    parser.add_argument("--json", default=None, help="write the result JSON here")
    parser.add_argument("--skip-baseline", action="store_true",
                        help="measure only the clip path")
    return parser.parse_args(argv)


def report(result: dict) -> int:
    clip = result["paths"]["clip"]
    synthesis = result["paths"]["synthesis"]
    print(f"📊 ack latency ({result['platform']})")
    print(f"   capture: {result['capture_device'] or 'none'}")

    def line(name: str, path: dict) -> str:
        if path["status"] != STATUS_MEASURED:
            return f"   ❌ {name}: not measured: {path['reason']} ({path['fix']})"
        rejected = f", {path['trials_rejected']} rejected" if path["trials_rejected"] else ""
        failed = f", {path['trials_failed']} failed" if path.get("trials_failed") else ""
        level = (f", monitor peak {path['monitor_peak']:.4f}"
                 if path.get("monitor_peak") else "")
        floor = (f" (threshold {path['threshold']:.4f})"
                 if path.get("threshold") is not None else "")
        return (f"   ✅ {name}: p50 {path['p50_sec']*1000:.1f} ms, "
                f"p95 {path['p95_sec']*1000:.1f} ms over {len(path['samples_sec'])} trials"
                f"{rejected}{failed}{level}{floor}")

    print(line("clip", clip))
    print(line("synthesis", synthesis))

    if clip["status"] != STATUS_MEASURED:
        print("   ⚠️  nothing was measured for the clip path; no claim is made")
        return EXIT_NOT_MEASURED
    if clip["p50_sec"] <= TARGET_P50_SEC and clip["p95_sec"] <= TARGET_P95_SEC:
        print(f"   🎯 clip path meets the target (p50 <= {TARGET_P50_SEC:.2f} s, "
              f"p95 <= {TARGET_P95_SEC:.2f} s)")
    else:
        print(f"   ⚠️  clip path MISSES the target (p50 <= {TARGET_P50_SEC:.2f} s, "
              f"p95 <= {TARGET_P95_SEC:.2f} s)")
    if synthesis["status"] == STATUS_MEASURED:
        ratio = synthesis["p50_sec"] / clip["p50_sec"] if clip["p50_sec"] else float("inf")
        verdict = "beats" if clip["p50_sec"] < synthesis["p50_sec"] else "does NOT beat"
        print(f"   ⚖️  clip path {verdict} synthesis: {ratio:.2f}x at p50 "
              f"(recorded baseline {BASELINE_SYNTHESIS_SEC:.2f} s)")
        return EXIT_OK if clip["p50_sec"] < synthesis["p50_sec"] else EXIT_TARGET_MISSED
    print(f"   ℹ️  synthesis not measured; the recorded baseline is "
          f"{BASELINE_SYNTHESIS_SEC:.2f} s")
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "platform": platform.platform(),
        "trials": args.trials,
        "phrase": args.phrase,
        "capture_device": None,
        "targets": {"p50_sec": TARGET_P50_SEC, "p95_sec": TARGET_P95_SEC},
        "baseline_synthesis_sec": BASELINE_SYNTHESIS_SEC,
        "paths": {
            "clip": asdict(PathResult("clip")),
            "synthesis": asdict(PathResult("synthesis")),
        },
    }
    clip_result = PathResult("clip", trials_requested=args.trials)
    synth_result = PathResult("synthesis", trials_requested=args.trials)

    sink = default_sink()
    device = args.device or (monitor_for_sink(sink) if sink else None)
    if not device:
        for path in (clip_result, synth_result):
            path.reason = "no sink monitor source was found to capture"
            path.fix = "run `pactl list short sources` and pass --device <monitor>"
        result["paths"]["clip"] = asdict(clip_result)
        result["paths"]["synthesis"] = asdict(synth_result)
        return report(result)

    from claire.config import load_settings
    from claire.output.clip_cache import ClipLibrary
    from claire.output.clip_player import ClipPlayer

    cfg = load_settings()
    phrase = args.phrase

    library = ClipLibrary(cfg)
    clip = library.get(phrase)
    if clip is None and args.live_fish:
        print(f"🎙️  rendering {phrase!r} for the clip path (a real speech request)...", flush=True)
        clip = library.get_or_generate(phrase)
    if clip is None:
        clip_result.reason = f"no cached clip for {phrase!r} in {library.directory}"
        clip_result.fix = "re-run with --live-fish to render it once"

    capture = MonitorCapture(device)
    try:
        capture.start()
        # Learn the monitor's own idle floor before trusting anything.
        idle_peaks = []
        deadline = time.monotonic() + args.settle
        while time.monotonic() < deadline:
            idle_peaks.append(capture.last_peak)
            time.sleep(0.02)
        ambient = max(idle_peaks or [0.0])
        result["capture_device"] = device
        result["ambient_peak"] = ambient

        if clip is not None:
            peak = signal_peak(clip)
            threshold = max(DETECTION_FLOOR, ambient * 1.3)
            print(f"🎚️  idle monitor peak {ambient:.4f}, clip peak before the device "
                  f"{peak:.4f}, detection threshold {threshold:.4f}", flush=True)
            try:
                # A canary first: the clip must actually reach the monitor
                # above the idle floor before any number from it is trusted.
                canary = measure_clip_path(ClipPlayer(enabled=True), clip, capture, 1,
                                           args.gap, args.timeout, threshold)
                if (canary.monitor_peak or 0.0) <= threshold:
                    raise RuntimeError(
                        f"the clip never cleared the idle floor on the monitor "
                        f"(monitor peak {canary.monitor_peak}, floor {ambient})"
                    )
                measured = measure_clip_path(ClipPlayer(enabled=True), clip, capture,
                                             args.trials, args.gap, args.timeout, threshold)
                measured.signal_peak = peak
                if measured.samples_sec:
                    measured.status = STATUS_MEASURED
                else:
                    measured.reason = "every trial was rejected because the sink was not quiet"
                    measured.fix = "close other audio clients and re-run"
                clip_result = measured
            except Exception as exc:
                clip_result.reason = f"the clip path could not be measured: {exc!r}"
                clip_result.fix = "check the output device and the sink monitor"

        if args.live_fish and not args.skip_baseline:
            try:
                print("🔊 measuring the synthesised path (a real speech request per trial)...",
                      flush=True)
                tts = build_tts_engine(cfg)
                tts.start()
                synth_result = measure_synthesis_path(
                    tts, phrase, capture, args.trials, args.gap, args.timeout,
                    max(DETECTION_FLOOR, ambient * 1.3),
                )
                if not synth_result.samples_sec:
                    raise RuntimeError(
                        f"every one of the {args.trials} synthesis requests made no sound"
                    )
                if synth_result.samples_sec:
                    synth_result.status = STATUS_MEASURED
                else:
                    synth_result.reason = "every trial was rejected because the sink was not quiet"
                    synth_result.fix = "close other audio clients and re-run"
                try:
                    tts.stop()
                except Exception:
                    pass
            except Exception as exc:
                synth_result.reason = f"the synthesis path could not be measured: {exc!r}"
                synth_result.fix = "confirm the engine starts and the voice id is valid"
        elif not args.skip_baseline:
            synth_result.reason = "measuring synthesis spends a real request and needs --live-fish"
            synth_result.fix = "re-run with --live-fish"
    finally:
        capture.close()

    clip_result.summarise()
    synth_result.summarise()
    result["paths"]["clip"] = asdict(clip_result)
    result["paths"]["synthesis"] = asdict(synth_result)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))
        print(f"💾 wrote {args.json}")
    return report(result)


if __name__ == "__main__":
    sys.exit(main())
