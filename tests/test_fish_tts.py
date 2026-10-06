"""Tests for :mod:`claire.output.fish_tts` — offline, no network, no sound device.

Every collaborator is injected: a fake client that yields bytes we choose, and a
fake player that records what it was handed. The suite pins the mechanisms that
make a streaming TTS engine feel right — and the failures each one prevents:

* sentence audio reaches the device strictly in order (a later sentence may be
  ready early, but must never start early);
* sentence N+1 is synthesised while sentence N is still playing (otherwise every
  full stop carries a network round trip);
* the queue ahead of the player is bounded (otherwise the whole reply is held in
  RAM before the first word);
* each chunk is copied into stable memory (a reused buffer cracks);
* WAV headers and headerless PCM both resolve to a real sample rate, and 48000
  is mapped to 44100;
* flush() stops playback in tens of milliseconds and is safe when idle;
* a missing or empty key file raises the named error quoting the path, and no
  audio is ever reported as spoken when it was not.
"""

from __future__ import annotations

import io
import os
import threading
import time
import wave
from types import SimpleNamespace

import pytest

from claire.output import fish_tts
from claire.output.fish_client import (
    FishKeyFileEmptyError,
    FishKeyFileMissingError,
)
from claire.output.fish_tts import (
    DEFAULT_LATENCY,
    MAX_QUEUED_AUDIO_BYTES,
    FishTTS,
    _coerce_sample_rate,
    _split_sentences,
)
from claire.output.tts import _preprocess_for_speech

# The gate runs `pytest -m unit`. A test file without this marker is
# silently DESELECTED, so it can pass forever without ever running.
pytestmark = pytest.mark.unit

TIMEOUT = 5.0


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class Recorder:
    """One ordered log shared by the fake client and the fake player."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events = []

    def add(self, kind, detail) -> None:
        with self.lock:
            self.events.append((kind, detail))

    def has(self, kind, detail) -> bool:
        with self.lock:
            return (kind, detail) in self.events

    def index(self, kind, detail) -> int:
        with self.lock:
            return self.events.index((kind, detail))

    def snapshot(self):
        with self.lock:
            return list(self.events)


def wait_until(predicate, timeout: float = TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class FakeClient:
    """Stands in for FishClient.synthesise: yields bytes, touches no socket."""

    def __init__(self, audio, chunk_size=None, recorder=None, on_synth=None) -> None:
        self._audio = audio
        self.chunk_size = chunk_size
        self.recorder = recorder
        self.on_synth = on_synth
        self.lock = threading.Lock()
        self.calls = []
        self.kwargs = []
        self.yielded = 0

    def synthesise(
        self,
        text,
        voice_id=None,
        latency="low",
        model="s1",
        first_chunk_timeout=8.0,
        total_timeout=120.0,
    ):
        with self.lock:
            self.calls.append(text)
            self.kwargs.append(
                {
                    "voice_id": voice_id,
                    "latency": latency,
                    "model": model,
                    "first_chunk_timeout": first_chunk_timeout,
                    "total_timeout": total_timeout,
                }
            )
        if self.recorder is not None:
            self.recorder.add("synth", text)
        if self.on_synth is not None:
            self.on_synth(text)
        if callable(self._audio):
            data = self._audio(text)
        else:
            data = self._audio.get(text, b"")
        if self.chunk_size:
            for start in range(0, len(data), self.chunk_size):
                with self.lock:
                    self.yielded += 1
                yield data[start:start + self.chunk_size]
        else:
            with self.lock:
                self.yielded += 1
            yield data


class FakePlayer:
    """Records what the engine hands it and can block the first write."""

    def __init__(self, recorder=None, block_first_write=None) -> None:
        self.recorder = recorder
        self.lock = threading.Lock()
        self.opened = []
        self.written = []  # raw objects as received: type is part of the contract
        self.abort_calls = 0
        self.close_calls = 0
        self.first_write_started = threading.Event()
        self._aborted = threading.Event()
        self._block_until = block_first_write
        self._open_count = 0

    def open(self, sample_rate) -> None:
        # Contract (same as _SoundDevicePlayer): (re)opening clears a previous
        # abort, so a restart after stop() can play again.
        self._aborted.clear()
        self._open_count += 1
        with self.lock:
            self.opened.append(sample_rate)
        if self.recorder is not None:
            self.recorder.add("open", sample_rate)

    def write(self, chunk) -> None:
        if self._open_count == 0:
            return  # like a real stream that was never (or no longer) open
        if self._block_until is not None and not self.first_write_started.is_set():
            self.first_write_started.set()
            self._block_until.wait(timeout=TIMEOUT)
        if self._aborted.is_set():
            return
        with self.lock:
            self.written.append(chunk)
        if self.recorder is not None:
            self.recorder.add("write", bytes(chunk))

    def abort(self) -> None:
        self.abort_calls += 1
        self._aborted.set()
        if self._block_until is not None:
            self._block_until.set()  # a real device write unblocks on abort()
        if self.recorder is not None:
            self.recorder.add("abort", None)

    def close(self) -> None:
        self.close_calls += 1
        self._open_count = 0


class FlakyOpenPlayer(FakePlayer):
    """Fails the first open() like a device that is momentarily busy."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_open = True

    def open(self, sample_rate) -> None:
        if self.fail_next_open:
            self.fail_next_open = False
            raise OSError("device busy")
        super().open(sample_rate)


class MutatingClient:
    """Yields the *same mutable buffer* twice, mutating it between yields.

    This is the shape a real streaming reader has (one read buffer, refilled per
    chunk); it is what makes the stable-copy requirement testable.
    """

    def __init__(self) -> None:
        self.texts = []

    def synthesise(self, text, **kwargs):
        self.texts.append(text)
        buf = bytearray(b"\x01" * 16)
        yield buf
        buf[:] = b"\x02" * 16
        yield buf


def make_wav(rate: int, payload: bytes, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(payload)
    return buf.getvalue()


def speak_and_wait(tts, text) -> bool:
    done = threading.Event()
    tts.speak(text, completion_callback=done.set)
    return done.wait(TIMEOUT)


# --------------------------------------------------------------------------
# Sentence chunking
# --------------------------------------------------------------------------

def test_sentences_split_at_every_full_stop_in_order():
    assert _split_sentences("One. Two. Three.") == ["One.", "Two.", "Three."]


def test_abbreviations_and_decimals_do_not_split():
    assert _split_sentences("Dr. Smith arrived. Then he left.") == [
        "Dr. Smith arrived.",
        "Then he left.",
    ]
    assert _split_sentences("The value is 3.14 exactly. Right.") == [
        "The value is 3.14 exactly.",
        "Right.",
    ]


def test_overlong_sentences_are_hard_split():
    pieces = _split_sentences("word " * 200)
    assert len(pieces) > 1
    assert all(len(piece) <= fish_tts.MAX_SENTENCE_CHARS for piece in pieces)


# --------------------------------------------------------------------------
# Ordering and prefetch
# --------------------------------------------------------------------------

def test_chunks_are_handed_to_the_player_in_order():
    audio = {"One.": b"\x01" * 32, "Two.": b"\x02" * 32, "Three.": b"\x03" * 32}
    client = FakeClient(audio)
    player = FakePlayer()
    tts = FishTTS(client=client, player=player)
    try:
        assert speak_and_wait(tts, "One. Two. Three.")
    finally:
        tts.stop()
    assert client.calls == ["One.", "Two.", "Three."]
    assert player.written == [b"\x01" * 32, b"\x02" * 32, b"\x03" * 32]


def test_later_sentence_is_synthesised_before_the_earlier_one_finishes_playing():
    recorder = Recorder()
    release = threading.Event()
    first, second = b"\x0a" * 16, b"\x0b" * 16
    client = FakeClient({"First sentence here.": first, "Second sentence here.": second},
                        recorder=recorder)
    player = FakePlayer(recorder=recorder, block_first_write=release)
    tts = FishTTS(client=client, player=player)
    done = threading.Event()
    try:
        tts.speak("First sentence here. Second sentence here.",
                  completion_callback=done.set)
        # The player is holding the *first* sentence's audio hostage here.
        assert player.first_write_started.wait(TIMEOUT)
        # Prefetch: the second sentence is already being synthesised even though
        # not one byte of the first has reached the device yet.
        assert wait_until(lambda: recorder.has("synth", "Second sentence here."))
        assert not recorder.has("write", first)
        release.set()
        assert done.wait(TIMEOUT)
    finally:
        tts.stop()
    # Strict order: the second sentence was *ready* first, yet the first sentence
    # is the one that was played first.
    assert recorder.index("synth", "Second sentence here.") < recorder.index("write", first)
    assert player.written == [first, second]


def test_audio_on_both_sides_of_a_flush_is_never_reordered_into_playback():
    # Two utterances, the second flushed away: nothing from the second may reach
    # the device after the first, and the first must still be complete.
    audio = {"First reply here.": b"\x21" * 8, "Second reply here.": b"\x22" * 8}
    release = threading.Event()
    client = FakeClient(audio)
    player = FakePlayer(block_first_write=release)
    tts = FishTTS(client=client, player=player)
    try:
        tts.speak("First reply here.")
        assert player.first_write_started.wait(TIMEOUT)
        tts.speak("Second reply here.")
        tts.flush()
        release.set()
        time.sleep(0.2)
        assert player.written == []
    finally:
        tts.stop()


def test_speaking_works_again_after_stop():
    # stop() leaves shutdown sentinels behind; a restarted engine must not
    # consume one and fall silent for every later reply.
    audio = {"First reply here.": b"\x31" * 8, "Second reply here.": b"\x32" * 8}
    client = FakeClient(audio)
    player = FakePlayer()
    tts = FishTTS(client=client, player=player)
    try:
        assert speak_and_wait(tts, "First reply here.")
        tts.stop()
        assert speak_and_wait(tts, "Second reply here.")
    finally:
        tts.stop()
    assert player.written == [b"\x31" * 8, b"\x32" * 8]


# --------------------------------------------------------------------------
# Backpressure
# --------------------------------------------------------------------------

def test_backpressure_caps_the_bytes_queued_ahead_of_the_player():
    chunk = b"\x00" * (64 * 1024)
    total_chunks = 12  # 768 KiB for one sentence: far more than the ceiling
    client = FakeClient(lambda text: chunk * total_chunks, chunk_size=len(chunk))
    release = threading.Event()
    player = FakePlayer(block_first_write=release)
    tts = FishTTS(client=client, player=player)
    try:
        tts.speak("This is a single long sentence.")
        # Nothing is draining (the player is blocked), so the producer must stop
        # at the ceiling. Wait for the yield count to stop growing.
        assert wait_until(
            lambda: client.yielded > 0
            and _stable(lambda: client.yielded, 0.15)
        )
        queued = tts.queued_audio_bytes()
        assert queued <= MAX_QUEUED_AUDIO_BYTES
        assert queued >= MAX_QUEUED_AUDIO_BYTES - 2 * len(chunk)
        # The whole reply was NOT materialised before playback started.
        assert client.yielded < total_chunks
    finally:
        release.set()
        tts.stop()


def _stable(reader, quiet_for: float) -> bool:
    """True when ``reader()`` has not changed for ``quiet_for`` seconds."""
    first = reader()
    time.sleep(quiet_for)
    return reader() == first


# --------------------------------------------------------------------------
# Stable copies
# --------------------------------------------------------------------------

def test_mutable_chunks_are_copied_before_they_reach_the_player():
    # MECHANISM 3: a bytearray refilled by the reader must not change under the
    # device. Without the copy the first chunk arrives as the mutated second one.
    client = MutatingClient()
    player = FakePlayer()
    tts = FishTTS(client=client, player=player)
    try:
        assert speak_and_wait(tts, "Buffered buffer here.")
    finally:
        tts.stop()
    assert player.written == [b"\x01" * 16, b"\x02" * 16]
    assert all(isinstance(chunk, bytes) for chunk in player.written)


# --------------------------------------------------------------------------
# Sample rate handling
# --------------------------------------------------------------------------

def test_48000_is_coerced_to_44100():
    assert _coerce_sample_rate(48000) == 44100
    assert _coerce_sample_rate(24000) == 24000
    assert _coerce_sample_rate(44100) == 44100
    assert _coerce_sample_rate("16000") == 16000
    assert _coerce_sample_rate(None) == 44100


@pytest.mark.parametrize("chunk_size", [None, 7])
def test_wav_header_stream_plays_at_the_declared_rate_without_the_header(chunk_size):
    payload = b"\x11\x22" * 500
    wav = make_wav(24000, payload)
    client = FakeClient({"Wav stream here.": wav}, chunk_size=chunk_size)
    player = FakePlayer()
    tts = FishTTS(client=client, player=player)
    try:
        assert speak_and_wait(tts, "Wav stream here.")
    finally:
        tts.stop()
    assert player.opened == [24000]
    assert b"".join(player.written) == payload
    assert sum(len(chunk) for chunk in player.written) == len(payload)


def test_wav_header_declaring_48000_plays_at_44100():
    payload = b"\x44" * 400
    client = FakeClient({"Unsupported rate here.": make_wav(48000, payload)})
    player = FakePlayer()
    tts = FishTTS(client=client, player=player)
    try:
        assert speak_and_wait(tts, "Unsupported rate here.")
    finally:
        tts.stop()
    assert player.opened == [44100]
    assert b"".join(player.written) == payload


def test_headerless_pcm_plays_at_the_default_rate():
    payload = b"\x33" * 4096
    client = FakeClient({"Raw pcm stream here.": payload}, chunk_size=1000)
    player = FakePlayer()
    tts = FishTTS(client=client, player=player)
    try:
        assert speak_and_wait(tts, "Raw pcm stream here.")
    finally:
        tts.stop()
    assert player.opened == [44100]
    assert b"".join(player.written) == payload


def test_short_first_chunk_that_could_be_a_riff_prefix_is_not_dropped():
    # 3 bytes cannot yet be told apart from "RIF": they must be held, not played
    # as audio and not discarded, until the next chunk resolves the format.
    payload = b"\x77" * 64
    client = FakeClient({"Prefix edge case here.": payload}, chunk_size=3)
    player = FakePlayer()
    tts = FishTTS(client=client, player=player)
    try:
        assert speak_and_wait(tts, "Prefix edge case here.")
    finally:
        tts.stop()
    assert b"".join(player.written) == payload


# --------------------------------------------------------------------------
# flush() / interrupt()
# --------------------------------------------------------------------------

def test_flush_is_safe_when_idle():
    tts = FishTTS(client=FakeClient({}), player=FakePlayer())
    tts.flush()
    tts.flush()
    assert tts.is_speaking() is False
    tts.start()
    tts.flush()
    assert tts.is_speaking() is False
    tts.stop()
    tts.flush()
    assert tts.is_speaking() is False


def test_flush_stops_playback_within_a_bounded_time():
    release = threading.Event()
    client = FakeClient({"Long first sentence here.": b"\x55" * 64})
    player = FakePlayer(block_first_write=release)
    tts = FishTTS(client=client, player=player)
    completed = []
    try:
        tts.speak("Long first sentence here.", completion_callback=lambda: completed.append(True))
        assert player.first_write_started.wait(TIMEOUT)
        started = time.monotonic()
        tts.flush()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        tts.stop()
    assert elapsed < 0.5, f"flush took {elapsed:.3f}s"
    assert tts.is_speaking() is False
    assert player.abort_calls >= 1
    time.sleep(0.1)
    assert player.written == []  # the blocked write never landed after the abort
    assert completed == []  # and no completion was claimed for audio never heard


def test_interrupt_delegates_to_flush():
    release = threading.Event()
    client = FakeClient({"Some sentence here.": b"\x66" * 32})
    player = FakePlayer(block_first_write=release)
    tts = FishTTS(client=client, player=player)
    try:
        tts.speak("Some sentence here.")
        assert player.first_write_started.wait(TIMEOUT)
        tts.interrupt()
        assert player.abort_calls == 1
        assert tts.is_speaking() is False
    finally:
        release.set()
        tts.stop()


def test_unplayed_text_is_not_reported_as_spoken():
    release = threading.Event()
    client = FakeClient({"Never heard this.": b"\x77" * 32})
    player = FakePlayer(block_first_write=release)
    tts = FishTTS(client=client, player=player)
    try:
        tts.speak("Never heard this.")
        assert player.first_write_started.wait(TIMEOUT)
        tts.flush()
        assert tts.get_last_spoken_text() == ""
    finally:
        release.set()
        tts.stop()


# --------------------------------------------------------------------------
# get_last_spoken_text / callback / config
# --------------------------------------------------------------------------

def test_last_spoken_text_reflects_what_was_played():
    client = FakeClient({"Hello there.": b"\x88" * 32})
    player = FakePlayer()
    tts = FishTTS(client=client, player=player)
    try:
        assert speak_and_wait(tts, "Hello there.")
        assert player.written  # it really was played
        assert tts.get_last_spoken_text() == _preprocess_for_speech("Hello there.")
        # is_speaking must clear once the utterance has been played out, or the
        # listener believes Claire is still talking for ever after her first reply.
        assert tts.is_speaking() is False
    finally:
        tts.stop()


def test_latency_defaults_to_low_and_normal_is_only_opt_in():
    assert DEFAULT_LATENCY == "low"
    client = FakeClient({"Hello there.": b"\x99" * 16})
    tts = FishTTS(client=client, player=FakePlayer(),
                  config=SimpleNamespace(fish_voice_id="voice-x", fish_model="s2"))
    try:
        assert speak_and_wait(tts, "Hello there.")
    finally:
        tts.stop()
    assert client.kwargs[0]["latency"] == "low"
    assert client.kwargs[0]["model"] == "s2"
    assert client.kwargs[0]["voice_id"] == "voice-x"

    normal_client = FakeClient({"Hello there.": b"\x99" * 16})
    tts = FishTTS(client=normal_client, player=FakePlayer(),
                  config=SimpleNamespace(fish_latency="normal", fish_voice_id="voice-x"))
    try:
        assert speak_and_wait(tts, "Hello there.")
    finally:
        tts.stop()
    assert normal_client.kwargs[0]["latency"] == "normal"


def test_first_chunk_and_total_budgets_are_passed_to_the_client():
    client = FakeClient({"Hello there.": b"\xaa" * 16})
    tts = FishTTS(client=client, player=FakePlayer())
    try:
        assert speak_and_wait(tts, "Hello there.")
    finally:
        tts.stop()
    assert client.kwargs[0]["first_chunk_timeout"] == fish_tts.FIRST_CHUNK_TIMEOUT
    assert client.kwargs[0]["total_timeout"] == fish_tts.TOTAL_TIMEOUT


def test_device_that_fails_to_open_does_not_kill_later_speech():
    # The failure it prevents: one bad device-open (or a busy device) taking the
    # playback thread down with it, so every later reply is silently never heard.
    client = FakeClient({"First reply here.": b"\x41" * 8, "Second reply here.": b"\x42" * 8})
    player = FlakyOpenPlayer()
    tts = FishTTS(client=client, player=player)
    try:
        # A failed utterance deliberately does NOT run its completion callback
        # (that is the N10 "never claim to have spoken" rule), so idle is what
        # this test waits for rather than completion.
        tts.speak("First reply here.")
        assert wait_until(lambda: not tts.is_speaking())
        # Nothing was heard and nothing is claimed to have been spoken.
        assert player.written == []
        assert tts.get_last_spoken_text() == ""
        # The loop survived, so the next reply is retried and does play.
        assert speak_and_wait(tts, "Second reply here.")
    finally:
        tts.stop()
    assert player.written == [b"\x42" * 8]
    assert player.opened == [44100]


# --------------------------------------------------------------------------
# Key file failures (N10): named error naming the path, never a silent success
# --------------------------------------------------------------------------

def test_missing_key_file_raises_named_error_naming_the_path(tmp_path):
    missing = tmp_path / "fish-keys.txt"
    tts = FishTTS(
        config=SimpleNamespace(fish_key_file=str(missing), fish_voice_id="voice-x")
    )
    with pytest.raises(FishKeyFileMissingError) as excinfo:
        tts.start()
    assert str(missing) in str(excinfo.value)
    assert tts.is_speaking() is False
    # speak() must surface the same named error rather than quietly not speaking.
    with pytest.raises(FishKeyFileMissingError) as excinfo:
        tts.speak("Hello there.")
    assert str(missing) in str(excinfo.value)
    assert tts.get_last_spoken_text() == ""


def test_empty_key_file_raises_named_error_naming_the_path(tmp_path):
    key_file = tmp_path / "fish-keys.txt"
    key_file.write_text("# only a comment, no keys\n\n")
    os.chmod(key_file, 0o600)
    tts = FishTTS(
        config=SimpleNamespace(fish_key_file=str(key_file), fish_voice_id="voice-x")
    )
    with pytest.raises(FishKeyFileEmptyError) as excinfo:
        tts.start()
    assert str(key_file) in str(excinfo.value)
    assert tts.is_speaking() is False
