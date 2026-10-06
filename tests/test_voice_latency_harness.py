"""Unit tests for ``scripts/measure_voice_latency.py`` (the latency harness).

These test the *harness's own logic*, not Claire's pipeline: every external
dependency (clock, sleep, recogniser, TTS engine, Fish probe, audio) is a fake,
so nothing here touches the network, a microphone, an audio device or a model.

The three properties the task requires are covered explicitly:

* a missing credential produces ``not measured`` (never a fabricated number),
* the false-positive list is reported exactly for a fake matcher,
* the ``--json`` output parses.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "measure_voice_latency.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("measure_voice_latency", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["measure_voice_latency"] = module
    spec.loader.exec_module(module)
    return module


h = _load_harness()

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeClock:
    """A clock that only moves when something sleeps or advances it."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class FakeIdentity:
    def describe(self) -> str:
        return "fake-streaming | kind=fake | path=<none>"


class FakeStreamingEngine:
    """Recogniser fake: text appears once speech has been seen.

    ``late_text`` is emitted only after ``late_after_silent_frames`` silent
    frames, which is how a real engine settles after the speech stops.
    """

    name = "fake-streaming"

    def __init__(
        self,
        text: str = "hello claire",
        late_text: str | None = None,
        late_after_silent_frames: int | None = None,
    ) -> None:
        self.text = text
        self.late_text = late_text
        self.late_after = late_after_silent_frames
        self.frames = 0
        self.silent_run = 0
        self.voiced = False
        self.finalized = None
        self.accepted: list[int] = []

    def reset(self) -> None:
        self.frames = 0
        self.silent_run = 0
        self.voiced = False
        self.finalized = None
        self.accepted = []

    def accept_waveform(self, samples, sample_rate) -> None:
        rms = float(np.sqrt(np.mean(np.asarray(samples, dtype=np.float64) ** 2)))
        self.frames += 1
        self.accepted.append(self.frames)
        if rms >= 0.01:
            self.voiced = True
            self.silent_run = 0
        else:
            self.silent_run += 1

    def partial_text(self) -> str:
        if not self.voiced:
            return ""
        if (
            self.late_text
            and self.late_after is not None
            and self.silent_run >= self.late_after
        ):
            return f"{self.text} {self.late_text}"
        return self.text

    def finalize(self) -> str:
        self.finalized = self.partial_text()
        return self.finalized

    def identity(self) -> FakeIdentity:
        return FakeIdentity()


class FakeBargeEngine(FakeStreamingEngine):
    """Emits a stop phrase only from frame ``stop_from_frame`` onwards."""

    def __init__(self, stop_from_frame: int = 2, text: str = "stop") -> None:
        super().__init__(text=text)
        self.stop_from_frame = stop_from_frame

    def partial_text(self) -> str:
        if not self.voiced or self.frames < self.stop_from_frame:
            return ""
        return self.text


class FakeTts:
    """Records speak/interrupt; ``is_speaking`` flips synchronously."""

    def __init__(self) -> None:
        self._speaking = False
        self.spoken: list[str] = []
        self.interrupts = 0

    def speak(self, text, completion_callback=None, duration_callback=None) -> None:
        self.spoken.append(text)
        self._speaking = True

    def interrupt(self) -> None:
        self.interrupts += 1
        self._speaking = False

    def is_speaking(self) -> bool:
        return self._speaking


def write_wav(
    path: Path,
    *,
    sample_rate: int = 16000,
    voiced_sec: float = 0.2,
    silence_sec: float = 0.9,
    amplitude: float = 0.3,
    channels: int = 1,
) -> Path:
    """A real 16-bit PCM wav: a tone for the voiced part, zeros after."""
    n_voiced = int(sample_rate * voiced_sec)
    n_silent = int(sample_rate * silence_sec)
    t = np.arange(n_voiced) / sample_rate
    tone = amplitude * np.sin(2.0 * np.pi * 220.0 * t)
    pcm = np.concatenate([tone, np.zeros(n_silent)]).astype(np.float32)
    data = np.clip(pcm * 32767.0, -32768, 32767).astype("<i2")
    if channels > 1:
        data = np.repeat(data[:, None], channels, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(data.tobytes())
    return path


def fake_ctx(**overrides) -> "h.Ctx":
    clock = overrides.pop("clock", None) or FakeClock()
    defaults = dict(clock=clock, sleep=clock.sleep)
    defaults.update(overrides)
    return h.Ctx(**defaults)


# --------------------------------------------------------------------------
# N10: a missing credential is "not measured", never a fabricated number
# --------------------------------------------------------------------------
def test_missing_fish_credential_is_not_measured_and_exits_nonzero(tmp_path, capsys):
    absent = tmp_path / "no-fish-key.txt"
    rc = h.main(
        [
            "--json",
            "--no-config",
            "--only",
            "first_audio_latency",
            "--fish-key-file",
            str(absent),
        ]
    )
    doc = json.loads(capsys.readouterr().out)
    measurement = doc["measurements"][0]

    assert rc == h.EXIT_NOT_MEASURED == 3
    assert measurement["status"] == "not_measured"
    assert measurement["value"] is None, "a missing credential must not become 0.0"
    assert measurement["method"], "the method that would have been used is still reported"
    assert str(absent) in measurement["detail"]
    assert "0600" in measurement["fix"]
    assert doc["all_measured"] is False
    assert doc["not_measured"] == ["first_audio_latency"]
    assert doc["exit_code"] == 3


def test_credential_present_without_backend_is_not_measured(tmp_path):
    key = tmp_path / "fish.key"
    key.write_text("placeholder-not-a-real-key\n", encoding="utf-8")
    ctx = fake_ctx(
        fish_key_file=key,
        fish_probe=None,
        fish_probe_error="Python module 'fish_audio_sdk' is not importable",
    )
    with pytest.raises(h.MeasurementUnavailable) as excinfo:
        h.measure_first_audio_latency(ctx)
    assert "fish_audio_sdk" in excinfo.value.reason
    assert "pip install" in excinfo.value.fix


def test_empty_credential_file_is_not_measured(tmp_path):
    key = tmp_path / "empty.key"
    key.write_text("", encoding="utf-8")
    ctx = fake_ctx(fish_key_file=key)
    with pytest.raises(h.CredentialMissingError) as excinfo:
        h.measure_first_audio_latency(ctx)
    assert str(key) in excinfo.value.reason


def test_offline_blocks_fish_measurements_without_touching_the_network(capsys):
    rc = h.main(
        [
            "--json",
            "--no-config",
            "--offline",
            "--only",
            "first_audio_latency,barge_in_latency",
        ]
    )
    doc = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert doc["not_measured"] == ["first_audio_latency", "barge_in_latency"]
    for measurement in doc["measurements"]:
        assert measurement["status"] == "not_measured"
        assert measurement["value"] is None
        assert "--offline" in measurement["detail"]


def test_barge_in_reports_every_missing_piece(tmp_path):
    ctx = fake_ctx(offline=True, barge_in_wav=tmp_path / "absent.wav")
    with pytest.raises(h.MeasurementUnavailable) as excinfo:
        h.measure_barge_in_latency(ctx)
    reason = excinfo.value.reason
    assert "--offline" in reason
    assert "barge-in wav" in reason
    assert "streaming recogniser" in reason


def test_silent_wav_is_not_measured_rather_than_zero(tmp_path):
    wav = write_wav(tmp_path / "silence.wav", voiced_sec=0.0, silence_sec=0.5)
    ctx = fake_ctx(wav_path=wav, streaming_engine=FakeStreamingEngine(), endpoint_runs=1)

    results = h.run_measurements(ctx, ["endpoint_latency"])
    measurement = results[0]
    assert measurement.status == "not_measured"
    assert measurement.value is None
    assert "RMS threshold" in measurement.detail


def test_endpoint_without_streaming_engine_names_the_missing_module(tmp_path):
    wav = write_wav(tmp_path / "speech.wav")
    ctx = fake_ctx(
        wav_path=wav,
        streaming_engine=None,
        streaming_engine_error=(
            "streaming engine module missing (No module named "
            "'claire.listening.stt.sherpa_streaming')"
        ),
    )
    results = h.run_measurements(ctx, ["endpoint_latency"])
    measurement = results[0]
    assert measurement.status == "not_measured"
    assert measurement.value is None
    assert "sherpa_streaming" in measurement.detail
    assert "sherpa_streaming" in measurement.fix


# --------------------------------------------------------------------------
# Wake false-positive rate: the exact list of words that woke her
# --------------------------------------------------------------------------
def test_false_positive_rate_lists_exactly_the_words_that_woke_her():
    words = ("clear", "clire", "window", "claira", "laptop")
    ctx = fake_ctx(
        wake_word="claire",
        negative_words=words,
        negative_words_generated=False,
        wake_matcher=lambda text: text in {"clire", "claira"},
    )
    measurement = h.measure_wake_false_positive_rate(ctx)

    assert measurement.measured
    assert measurement.value == pytest.approx(2 / 5)
    assert measurement.unit == "rate"
    assert measurement.raw["woke_count"] == 2
    assert measurement.raw["total_phrases"] == 5
    assert measurement.raw["woke_words"] == ["clire", "claira"]
    assert measurement.raw["clean_words"] == ["clear", "window", "laptop"]
    assert "clire" in measurement.detail


def test_false_positive_rate_zero_is_still_a_number_and_lists_nothing():
    ctx = fake_ctx(
        wake_word="claire",
        negative_words=("clear", "window"),
        wake_matcher=lambda text: False,
    )
    measurement = h.measure_wake_false_positive_rate(ctx)
    assert measurement.measured
    assert measurement.value == 0.0
    assert measurement.raw["woke_words"] == []
    assert "woke: none" in measurement.detail


def test_generated_negative_list_is_deterministic_and_excludes_true_positives():
    first = h.generate_negative_words("claire", 30, seed=7)
    second = h.generate_negative_words("claire", 30, seed=7)

    assert first == second
    assert len(first) == 30
    assert all(not h._is_plain_name(word, "claire", ()) for word in first)
    assert "claire" not in {word.casefold() for word in first}


def test_read_word_list_skips_comments_and_blanks_and_names_missing_file(tmp_path):
    path = tmp_path / "words.txt"
    path.write_text("# comment\n\nclire\n  window  # trailing\n", encoding="utf-8")
    assert h.read_word_list(path, what="negative") == ("clire", "window")

    missing = tmp_path / "absent.txt"
    with pytest.raises(h.MeasurementUnavailable) as excinfo:
        h.read_word_list(missing, what="negative")
    assert str(missing) in excinfo.value.reason


# --------------------------------------------------------------------------
# Wake latency
# --------------------------------------------------------------------------
def test_wake_latency_is_the_median_matcher_cost():
    clock = FakeClock()

    def matcher(text: str) -> bool:
        clock.advance(0.002 if text.startswith("claire") else 0.004)
        return True

    ctx = fake_ctx(
        clock=clock,
        wake_words=("claire", "hey claire", "claire, are you there"),
        wake_matcher=matcher,
    )
    measurement = h.measure_wake_latency(ctx)

    assert measurement.measured
    assert measurement.value == pytest.approx(0.002)
    assert measurement.raw["phrases"] == 3
    assert measurement.raw["detection_rate"] == 1.0
    assert measurement.raw["detected_words"] == ["claire", "hey claire", "claire, are you there"]


def test_wake_latency_default_phrase_list_needs_no_audio():
    phrases = h.default_wake_phrases("claire", ("clare",))
    assert phrases[0] == "claire"
    assert "clare" in phrases
    assert len(phrases) == len({p.casefold() for p in phrases})


# --------------------------------------------------------------------------
# Endpoint latency
# --------------------------------------------------------------------------
def test_endpoint_latency_measures_the_endpoint_window(tmp_path):
    wav = write_wav(tmp_path / "speech.wav", voiced_sec=0.2, silence_sec=0.9)
    clock = FakeClock()
    engine = FakeStreamingEngine()
    ctx = fake_ctx(
        clock=clock,
        wav_path=wav,
        streaming_engine=engine,
        frame_ms=20,
        endpoint_silence_ms=800,
        endpoint_runs=2,
    )
    measurement = h.measure_endpoint_latency(ctx)

    assert measurement.measured
    assert measurement.value == pytest.approx(0.8, abs=1e-6)
    assert measurement.raw["sample_rate"] == 16000
    run = measurement.raw["runs"][0]
    assert run["endpoint_reached_in_wav"] is True
    assert run["transcript"] == "hello claire"
    assert run["transcript_settle_sec"] == pytest.approx(0.0)
    assert run["endpoint_window_sec"] == pytest.approx(0.8)
    assert measurement.raw["engine_identity"].startswith("fake-streaming")


def test_endpoint_latency_reports_late_settle_after_speech_stops(tmp_path):
    wav = write_wav(tmp_path / "speech.wav", voiced_sec=0.2, silence_sec=0.9)
    clock = FakeClock()
    engine = FakeStreamingEngine(late_text="today", late_after_silent_frames=2)
    ctx = fake_ctx(
        clock=clock,
        wav_path=wav,
        streaming_engine=engine,
        frame_ms=20,
        endpoint_silence_ms=800,
        endpoint_runs=1,
    )
    measurement = h.measure_endpoint_latency(ctx)
    settle = measurement.raw["runs"][0]["transcript_settle_sec"]
    # The late text appears on the second silent frame after speech stopped.
    assert settle == pytest.approx(0.04, abs=1e-9)


def test_endpoint_unpaced_run_excludes_the_audio_window(tmp_path):
    wav = write_wav(tmp_path / "speech.wav", voiced_sec=0.2, silence_sec=0.9)
    clock = FakeClock()
    ctx = fake_ctx(
        clock=clock,
        wav_path=wav,
        streaming_engine=FakeStreamingEngine(),
        frame_ms=20,
        endpoint_silence_ms=800,
        endpoint_runs=1,
        pace=False,
    )
    measurement = h.measure_endpoint_latency(ctx)
    assert measurement.measured
    assert measurement.value == pytest.approx(0.0, abs=1e-9)
    assert measurement.raw["paced"] is False


def test_endpoint_latency_resamples_a_foreign_sample_rate(tmp_path):
    wav = write_wav(tmp_path / "speech8k.wav", sample_rate=8000, voiced_sec=0.2, silence_sec=0.9)
    clock = FakeClock()
    ctx = fake_ctx(
        clock=clock,
        wav_path=wav,
        streaming_engine=FakeStreamingEngine(),
        frame_ms=20,
        endpoint_silence_ms=800,
        endpoint_runs=1,
    )
    audio, rate = h.read_wav_mono(wav, 16000)
    assert rate == 16000
    assert audio.dtype == np.float32
    # 1.1 s of 8 kHz audio becomes ~1.1 s at 16 kHz.
    assert audio.size == pytest.approx(1.1 * 16000, rel=0.01)

    measurement = h.measure_endpoint_latency(ctx)
    assert measurement.measured
    assert measurement.raw["sample_rate"] == 16000


def test_read_wav_mono_names_a_missing_file(tmp_path):
    with pytest.raises(h.MeasurementUnavailable) as excinfo:
        h.read_wav_mono(tmp_path / "absent.wav", 16000)
    assert "absent.wav" in excinfo.value.reason


# --------------------------------------------------------------------------
# Barge-in latency (fake engine + fake TTS: no Fish call, no audio device)
# --------------------------------------------------------------------------
def test_barge_in_latency_from_first_voiced_frame_to_stopped_playback(tmp_path):
    key = tmp_path / "fish.key"
    key.write_text("placeholder\n", encoding="utf-8")
    wav = write_wav(tmp_path / "stop.wav", voiced_sec=0.4, silence_sec=0.1)
    clock = FakeClock()
    tts = FakeTts()
    # The recogniser only produces "stop" on the third frame, so the measured
    # value must run from the *first voiced frame*, not from the detection frame.
    engine = FakeBargeEngine(stop_from_frame=3)
    ctx = fake_ctx(
        clock=clock,
        fish_key_file=key,
        tts_engine=tts,
        streaming_engine=engine,
        barge_in_wav=wav,
        frame_ms=20,
        barge_in_runs=1,
    )
    measurement = h.measure_barge_in_latency(ctx)

    assert measurement.measured
    assert measurement.value == pytest.approx(0.04, abs=1e-9)
    assert tts.spoken and tts.interrupts >= 1
    assert measurement.raw["tts_engine"] == "FakeTts"


def test_barge_in_without_a_recognised_phrase_is_not_measured(tmp_path):
    key = tmp_path / "fish.key"
    key.write_text("placeholder\n", encoding="utf-8")
    wav = write_wav(tmp_path / "noise.wav", voiced_sec=0.4, silence_sec=0.1)
    engine = FakeBargeEngine(stop_from_frame=10_000)
    ctx = fake_ctx(
        fish_key_file=key,
        tts_engine=FakeTts(),
        streaming_engine=engine,
        barge_in_wav=wav,
        frame_ms=20,
        barge_in_runs=1,
        stop_matcher=lambda text: False,
    )
    with pytest.raises(h.MeasurementUnavailable) as excinfo:
        h.measure_barge_in_latency(ctx)
    assert "no stop/wake phrase" in excinfo.value.reason


# --------------------------------------------------------------------------
# JSON output, secrets, CLI plumbing
# --------------------------------------------------------------------------
def test_json_output_parses_and_never_prints_the_secret(tmp_path, capsys, monkeypatch):
    secret = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"
    key = tmp_path / "fish.key"
    key.write_text(secret + "\n", encoding="utf-8")
    (tmp_path / "fake_probe.py").write_text(
        "def probe(text, runs, key_file):\n    return [0.0123, 0.0098][:runs]\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    rc = h.main(
        [
            "--json",
            "--no-config",
            "--only",
            "first_audio_latency",
            "--fish-key-file",
            str(key),
            "--fish-probe-module",
            "fake_probe:probe",
            "--fish-runs",
            "2",
        ]
    )
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    measurement = doc["measurements"][0]

    assert rc == 0
    assert measurement["status"] == "measured"
    assert measurement["value"] == pytest.approx(0.01105, abs=1e-9)
    assert secret not in captured.out
    assert secret not in captured.err
    assert "present" in measurement["raw"]["credential_file"]
    assert "value never read" in measurement["raw"]["credential_file"]


def test_offline_json_report_parses_with_real_matcher(capsys):
    rc = h.main(
        [
            "--json",
            "--no-config",
            "--offline",
            "--only",
            "wake_latency,wake_false_positive_rate",
            "--generate-negative-words",
            "12",
        ]
    )
    doc = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert doc["schema"] == h.SCHEMA
    assert doc["exit_code"] == 0
    assert doc["all_measured"] is True
    assert [m["key"] for m in doc["measurements"]] == [
        "wake_latency",
        "wake_false_positive_rate",
    ]
    for measurement in doc["measurements"]:
        assert measurement["status"] == "measured"
        assert measurement["value"] is not None
        assert measurement["method"], "every measurement names its method"
    fp = doc["measurements"][1]
    assert fp["raw"]["total_phrases"] == 12
    assert 0.0 <= fp["value"] <= 1.0
    assert len(fp["raw"]["woke_words"]) == fp["raw"]["woke_count"]
    # The generated list must be reproducible from the report alone.
    assert h.generate_negative_words(
        fp["raw"]["wake_word"],
        fp["raw"]["total_phrases"],
        seed=fp["raw"]["seed"],
        aliases=tuple(fp["raw"]["aliases"]),
    ) == tuple(fp["raw"]["word_list"])


def test_skipped_measurements_do_not_affect_the_exit_code(capsys):
    rc = h.main(
        [
            "--json",
            "--quiet",
            "--no-config",
            "--offline",
            "--skip",
            "wake_latency",
        ]
    )
    doc = json.loads(capsys.readouterr().out)
    assert "wake_latency" not in [m["key"] for m in doc["measurements"]]
    assert rc == 3, "the Fish measurements were requested and not taken"
    assert "wake_latency" not in doc["not_measured"]


def test_cfg_overlay_overrides_only_the_named_field():
    base = h._SimpleCfg({"streaming_stt_model_dir": "/base", "sample_rate": 8000})
    overlay = h.CfgOverlay(base, streaming_stt_model_dir="/other")

    assert overlay.streaming_stt_model_dir == "/other"
    assert overlay.sample_rate == 8000
    assert base.streaming_stt_model_dir == "/base", "the user's config is never mutated"
    inner = overlay.overlay(endpoint_silence_ms=500)
    assert inner.streaming_stt_model_dir == "/other"
    assert inner.endpoint_silence_ms == 500


def test_streaming_model_dir_flag_reaches_the_engine_factory(monkeypatch, capsys):
    seen: dict = {}

    def fake_factory(cfg, defaults):
        seen["model_dir"] = getattr(cfg, "streaming_stt_model_dir", None)
        seen["sample_rate"] = getattr(cfg, "sample_rate", None)
        return None, "fake factory for the test"

    monkeypatch.setattr(h, "try_create_streaming_engine", fake_factory)
    rc = h.main(
        [
            "--json",
            "--quiet",
            "--no-config",
            "--no-isolate",
            "--offline",
            "--only",
            "endpoint_latency",
            "--streaming-model-dir",
            "/tmp/pretend-model",
            "--wav",
            "/tmp/pretend.wav",
        ]
    )
    doc = json.loads(capsys.readouterr().out)

    assert seen["model_dir"] == "/tmp/pretend-model"
    assert seen["sample_rate"] == 16000
    assert rc == 3
    assert doc["measurements"][0]["status"] == "not_measured"
    assert doc["measurements"][0]["value"] is None


def test_endpoint_measurement_is_isolated_so_a_native_abort_cannot_lose_the_report(tmp_path, capfd):
    """The default path runs the native-driving measurement in a child process.

    Here the child fails honestly (absent model dir) rather than aborting, but
    the plumbing is the same one that turns a native ``LOG(FATAL)`` into a
    "not measured" line instead of a lost report. ``capfd`` is used because the
    child writes to the inherited file descriptor.
    """
    wav = write_wav(tmp_path / "speech.wav")
    rc = h.main(
        [
            "--json",
            "--quiet",
            "--no-config",
            "--offline",
            "--only",
            "endpoint_latency",
            "--streaming-model-dir",
            "/tmp/definitely-absent-sherpa-model-xyz",
            "--wav",
            str(wav),
        ]
    )
    doc = json.loads(capfd.readouterr().out)
    measurement = doc["measurements"][0]

    assert rc == 3
    assert measurement["key"] == "endpoint_latency"
    assert measurement["status"] == "not_measured"
    assert measurement["value"] is None
    assert "absent-sherpa-model-xyz" in measurement["detail"]
    assert doc["all_measured"] is False


def test_isolated_measurement_report_never_fabricates_on_a_bad_child(monkeypatch):
    class DeadProcess:
        returncode = 255
        stdout = ""
        stderr = "FATAL: /project/sherpa-onnx/csrc/features.cc:GetFrames:188 0 + 39 > 1\n"

    def dead_runner(argv, capture_output, text, timeout):
        return DeadProcess()

    measurement = h.isolated_measurement(
        "endpoint_latency", ["--only", "endpoint_latency"], timeout=5.0, runner=dead_runner
    )
    assert measurement.status == "not_measured"
    assert measurement.value is None
    assert "255" in measurement.detail
    assert "features.cc" in measurement.detail
    assert "--frame-ms 400" in measurement.fix
    assert "sherpa_streaming.py" in measurement.fix


def test_isolated_measurement_returns_the_childs_measurement(monkeypatch):
    child = h.Measurement(
        key="endpoint_latency",
        title="endpoint latency",
        method=h.ENDPOINT_METHOD,
        status="measured",
        value=0.812,
        unit="seconds",
        raw={"runs": []},
    )

    class FakeProcess:
        returncode = 0
        stdout = json.dumps(child.to_json()) + "\n"
        stderr = ""

    measurement = h.isolated_measurement(
        "endpoint_latency",
        ["--only", "endpoint_latency"],
        timeout=5.0,
        runner=lambda argv, capture_output, text, timeout: FakeProcess(),
    )
    assert measurement.measured
    assert measurement.value == pytest.approx(0.812)


def test_dump_word_lists_writes_the_exact_lists_used(tmp_path, capsys):
    dump = tmp_path / "lists"
    rc = h.main(
        [
            "--json",
            "--quiet",
            "--no-config",
            "--offline",
            "--only",
            "wake_false_positive_rate",
            "--generate-negative-words",
            "8",
            "--dump-word-lists",
            str(dump),
        ]
    )
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    negative = (dump / "negative_words.txt").read_text(encoding="utf-8").split()
    wake = (dump / "wake_words.txt").read_text(encoding="utf-8").split("\n")
    assert negative == doc["measurements"][0]["raw"]["word_list"]
    assert wake[0] == "claire"


def test_unknown_measurement_key_is_fatal(tmp_path, capsys):
    rc = h.main(["--no-config", "--only", "not_a_measurement"])
    assert rc == h.EXIT_FATAL == 1
    assert "unknown measurement key" in capsys.readouterr().err


def test_missing_word_list_file_is_fatal_not_silently_ignored(tmp_path, capsys):
    absent = tmp_path / "no-such-list.txt"
    rc = h.main(
        ["--no-config", "--only", "wake_false_positive_rate", "--negative-words-file", str(absent)]
    )
    assert rc == h.EXIT_FATAL == 1
    assert str(absent) in capsys.readouterr().err


def test_table_shows_status_value_method_and_raw_values(tmp_path):
    absent = tmp_path / "absent.key"
    ctx = fake_ctx(
        fish_key_file=absent,
        negative_words=("clear",),
        wake_matcher=lambda text: False,
        wake_words=("claire",),
    )
    measurements = h.run_measurements(ctx, ["first_audio_latency", "wake_false_positive_rate"])
    report = h.Report(
        measurements=measurements,
        host="mana-pc",
        repo_root=str(ROOT),
        when_utc="2025-01-01T00:00:00Z",
        python="3.12.0",
        platform="Linux-test",
    )
    table = report.render_table()

    assert "NOT MEASURED" in table
    assert "0.0000" in table  # the measured rate is printed with its raw value
    assert "Raw values" in table
    assert "wake_false_positive_rate" in table
    assert "NOT MEASURED (no number invented -- N10)" in table
    assert report.exit_code == h.EXIT_NOT_MEASURED
