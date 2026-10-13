"""Behavioural tests for the pre-recorded speech-clip cache.

The cache renders a phrase once and reuses the file on disk. These tests pin
the behaviour that matters:

- the cache key changes when the voice changes, so a clip rendered in one
  voice is never played in another;
- a phrase is synthesised once and then served from disk;
- a synthesis failure leaves no file and is reported as "no clip" rather than
  raising into the caller, because the caller falls back to live synthesis;
- least-recently-used phrase selection.

No audio device and no network are involved: synthesis is injected.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from claire.output import clip_cache
from claire.output.clip_cache import (
    Clip,
    ClipLibrary,
    clip_cache_dir,
    clip_cache_key,
    load_clip,
    voice_fingerprint,
    write_clip,
)

pytestmark = pytest.mark.unit


def fish_config(**overrides):
    values = dict(
        tts_engine="fish",
        fish_voice_id="voice-one",
        fish_model="s1",
        tts_voice=None,
        voice_debug=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def tone(rate=22050, seconds=0.05, value=1000):
    return np.full(int(rate * seconds), value, dtype=np.int16)


# --- Cache key and location -------------------------------------------------


def test_cache_key_is_stable_for_the_same_voice_and_phrase():
    a = clip_cache_key("On it.", voice_fingerprint(fish_config()))
    b = clip_cache_key("On it.", voice_fingerprint(fish_config()))
    assert a == b


def test_cache_key_changes_with_the_voice():
    one = clip_cache_key("On it.", voice_fingerprint(fish_config()))
    other = clip_cache_key("On it.", voice_fingerprint(fish_config(fish_voice_id="voice-two")))
    assert one != other, "a clip rendered in one voice must not be served for another"


def test_cache_key_changes_with_the_phrase():
    voice = voice_fingerprint(fish_config())
    assert clip_cache_key("On it.", voice) != clip_cache_key("Okay.", voice)


def test_voice_fingerprint_changes_with_the_model():
    one = voice_fingerprint(fish_config())
    other = voice_fingerprint(fish_config(fish_model="s2.1-pro-free"))
    assert one != other


def test_piper_voice_fingerprint_tracks_the_model_path():
    one = voice_fingerprint(SimpleNamespace(tts_engine="piper", tts_piper_model_path="/v/a.onnx"))
    other = voice_fingerprint(SimpleNamespace(tts_engine="piper", tts_piper_model_path="/v/b.onnx"))
    assert one != other


def test_cache_dir_defaults_under_the_claire_cache():
    directory = clip_cache_dir(env={"HOME": "/home/someone", "XDG_CACHE_HOME": ""})
    assert str(directory).endswith("/.cache/claire/clips")


def test_cache_dir_honours_the_xdg_override():
    directory = clip_cache_dir(env={"XDG_CACHE_HOME": "/tmp/xdg"})
    assert str(directory) == "/tmp/xdg/claire/clips"


def test_cache_dir_honours_the_explicit_override():
    directory = clip_cache_dir(env={clip_cache.CLIP_CACHE_DIR_ENV: "/tmp/clips-here"})
    assert str(directory) == "/tmp/clips-here"


# --- On-disk round trip -----------------------------------------------------


def test_clip_round_trips_through_disk(tmp_path):
    path = tmp_path / "clip.wav"
    samples = tone(value=1234)
    write_clip(path, samples, 22050)

    clip = load_clip(path, phrase="On it.")
    assert clip is not None
    assert clip.sample_rate == 22050
    assert clip.phrase == "On it."
    np.testing.assert_array_equal(clip.samples, samples)


def test_load_clip_returns_none_for_a_missing_file(tmp_path):
    assert load_clip(tmp_path / "absent.wav") is None


def test_load_clip_returns_none_for_a_corrupt_file(tmp_path):
    path = tmp_path / "bad.wav"
    path.write_bytes(b"not a wav at all")
    assert load_clip(path) is None


# --- Library behaviour ------------------------------------------------------


class RecordingSynth:
    def __init__(self, value=2000, exc=None):
        self.calls = []
        self.value = value
        self.exc = exc

    def __call__(self, phrase):
        self.calls.append(phrase)
        if self.exc is not None:
            raise self.exc
        return tone(value=self.value), 22050


def test_library_synthesises_once_then_serves_from_disk(tmp_path):
    synth = RecordingSynth()
    library = ClipLibrary(fish_config(), synth=synth, directory=tmp_path)

    first = library.get_or_generate("On it.")
    second = library.get_or_generate("On it.")

    assert first is not None and second is not None
    assert synth.calls == ["On it."]
    assert library.path_for("On it.").exists()
    np.testing.assert_array_equal(first.samples, second.samples)


def test_library_serves_an_existing_file_without_synthesis(tmp_path):
    synth = RecordingSynth()
    library = ClipLibrary(fish_config(), synth=synth, directory=tmp_path)
    write_clip(library.path_for("On it."), tone(), 22050)

    clip = library.get("On it.")

    assert clip is not None
    assert synth.calls == []


def test_library_get_does_not_generate_missing_clips(tmp_path):
    synth = RecordingSynth()
    library = ClipLibrary(fish_config(), synth=synth, directory=tmp_path)

    assert library.get("On it.") is None
    assert synth.calls == [], "the hot path must never block on synthesis"


def test_library_reports_no_clip_when_synthesis_fails(tmp_path):
    synth = RecordingSynth(exc=RuntimeError("fish is down"))
    library = ClipLibrary(fish_config(), synth=synth, directory=tmp_path)

    assert library.get_or_generate("On it.") is None
    assert not library.path_for("On it.").exists()


def test_a_clip_rendered_for_one_voice_is_not_reused_for_another(tmp_path):
    quiet = RecordingSynth(value=100)
    loud = RecordingSynth(value=9000)
    first = ClipLibrary(fish_config(), synth=quiet, directory=tmp_path)
    second = ClipLibrary(
        fish_config(fish_voice_id="voice-two"), synth=loud, directory=tmp_path
    )

    clip_one = first.get_or_generate("On it.")
    clip_two = second.get_or_generate("On it.")

    assert clip_one is not None and clip_two is not None
    assert int(clip_two.samples[0]) == 9000, "the second voice must be re-rendered"
    assert first.path_for("On it.") != second.path_for("On it.")


def test_prewarm_generates_missing_clips_off_the_calling_thread(tmp_path):
    release = threading.Event()
    synth = RecordingSynth()

    def slow(phrase):
        release.wait(2.0)
        return synth(phrase)

    library = ClipLibrary(fish_config(), synth=slow, directory=tmp_path)
    thread = library.prewarm(["On it.", "Okay."])
    assert thread is not None
    release.set()
    thread.join(2.0)

    assert not thread.is_alive()
    assert sorted(synth.calls) == ["Okay.", "On it."]


def test_prewarm_does_not_raise_when_synthesis_fails(tmp_path):
    synth = RecordingSynth(exc=RuntimeError("no network"))
    library = ClipLibrary(fish_config(), synth=synth, directory=tmp_path)

    thread = library.prewarm(["On it."])
    thread.join(2.0)

    assert not thread.is_alive()
    assert library.get("On it.") is None


def test_prewarm_skips_phrases_that_are_already_cached(tmp_path):
    synth = RecordingSynth()
    library = ClipLibrary(fish_config(), synth=synth, directory=tmp_path)
    write_clip(library.path_for("On it."), tone(), 22050)

    thread = library.prewarm(["On it."])

    assert thread is None, "nothing to render means no thread"
    assert synth.calls == []


def test_available_returns_only_clips_present_on_disk(tmp_path):
    library = ClipLibrary(fish_config(), synth=RecordingSynth(), directory=tmp_path)
    write_clip(library.path_for("On it."), tone(), 22050)
    write_clip(library.path_for("Okay."), tone(), 22050)

    available = library.available(["On it.", "Okay.", "Right away."])

    assert [clip.phrase for clip in available] == ["On it.", "Okay."]


# --- Least-recently-used phrase selection -----------------------------------


def test_least_recently_used_picks_the_front_of_the_order():
    from claire.listening.ack_continuations import pick_least_recently_used

    assert pick_least_recently_used(["a", "b", "c"]) == "a"


def test_least_recently_used_skips_phrases_already_spoken_this_turn():
    from claire.listening.ack_continuations import pick_least_recently_used

    assert pick_least_recently_used(["a", "b", "c"], exclude={"a"}) == "b"
    assert pick_least_recently_used(["a", "b", "c"], exclude={"a", "b"}) == "c"


def test_least_recently_used_falls_back_when_every_phrase_is_spoken():
    from claire.listening.ack_continuations import pick_least_recently_used

    assert pick_least_recently_used(["a", "b"], exclude={"a", "b"}) == "a"


def test_least_recently_used_handles_an_empty_order():
    from claire.listening.ack_continuations import pick_least_recently_used

    assert pick_least_recently_used([]) is None
