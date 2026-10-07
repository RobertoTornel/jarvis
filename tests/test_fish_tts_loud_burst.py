"""Regression tests for the intermittent full-scale burst the owner hears.

The bug is not a volume setting. It is that the playback layer wrote *whatever
bytes arrived* into a stream it had opened as signed 16-bit mono, without ever
verifying that the bytes were signed 16-bit mono PCM. Reinterpreted, the wrong
bytes are not quiet - they are full scale:

* an MP3 body (Fish Audio's default response, whatever ``format`` asks for)
  reads back with a peak of 32768/32768 = 0.0 dBFS and an RMS about 27 dB
  above her normal speech, measured live against the same sentence;
* a RIFF container stdlib ``wave`` will not open (IEEE-float WAVE) used to be
  declared "headerless PCM" once it passed the 64 KiB sniff budget, so the
  container header itself - ``RIFF`` is 0x4952/0x4646 - was played at full scale.

Both are intermittent: they depend on exactly what the server returns for a
given sentence. Both are proven here at the engine boundary, and both must now
end as a *reported failure* with nothing written to the device.

The other defect pinned here is silent: ``RawOutputStream.write`` raises
``ValueError`` for a buffer that is not a whole number of frames, and the player
caught and discarded that, so the audio vanished with nothing reported.
"""

from __future__ import annotations

import io
import struct
import sys
import time
import types

import numpy as np
import pytest

from claire.output import fish_tts
from claire.output.fish_client import FishUnsupportedAudioFormatError
from claire.output.fish_tts import (
    MAX_WAV_HEADER_BYTES,
    PLAYBACK_FRAME_BYTES,
    FishTTS,
    _PcmStreamFormat,
    _limit_peak,
)

# The gate runs `pytest -m unit`. A test file without this marker is
# silently DESELECTED, so it can pass forever without ever running.
pytestmark = pytest.mark.unit

TIMEOUT = 5.0

SENTENCE = "This is a burst test."

#: A real MP3 frame header, exactly the bytes Fish Audio returned for the same
#: sentence when `format` was left at its default.
MP3_BODY = b"\xff\xfb\x90\xc4" + b"\x00\x00\x00\x00" + bytes(range(256)) * 8


class _FakeClient:
    def __init__(self, data: bytes, chunk_size: int | None = None) -> None:
        self.data = data
        self.chunk_size = chunk_size

    def synthesise(self, text, **kwargs):
        if self.chunk_size:
            for start in range(0, len(self.data), self.chunk_size):
                yield self.data[start:start + self.chunk_size]
        else:
            yield self.data


class _RecordingPlayer:
    """Records exactly what the engine hands the device."""

    def __init__(self) -> None:
        self.opened: list[int] = []
        self.written: list[bytes] = []

    def open(self, sample_rate) -> None:
        self.opened.append(sample_rate)

    def write(self, chunk) -> None:
        self.written.append(bytes(chunk))

    def abort(self) -> None:
        pass

    def close(self) -> None:
        pass


def _wav(rate: int, payload: bytes, channels: int = 1, width: int = 2,
         fmt_tag: int = 1) -> bytes:
    """A RIFF/WAVE container with an arbitrary declared layout."""
    buf = io.BytesIO()
    block_align = channels * width
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(payload)))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))
    buf.write(struct.pack("<H", fmt_tag))
    buf.write(struct.pack("<H", channels))
    buf.write(struct.pack("<I", rate))
    buf.write(struct.pack("<I", rate * block_align))
    buf.write(struct.pack("<H", block_align))
    buf.write(struct.pack("<H", width * 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(payload)))
    buf.write(payload)
    return buf.getvalue()


def _peak(raw: bytes) -> int:
    if len(raw) < 2:
        return 0
    samples = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2")
    return int(np.abs(samples.astype(np.int32)).max()) if samples.size else 0


def _run_to_idle(data: bytes, chunk_size: int | None = None):
    """Speak one sentence through the real engine and wait for it to settle."""
    player = _RecordingPlayer()
    tts = FishTTS(client=_FakeClient(data, chunk_size), player=player)
    try:
        tts.speak(SENTENCE)
        deadline = time.monotonic() + TIMEOUT
        while tts.is_speaking() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        tts.stop()
    return player


# --------------------------------------------------------------------------
# What the bug actually does: the bytes are full scale when reinterpreted
# --------------------------------------------------------------------------

def test_an_mp3_body_reinterpreted_as_int16_is_a_full_scale_burst():
    """Pin the mechanism, so the guard below cannot look like an overreaction."""
    samples = np.frombuffer(MP3_BODY[: len(MP3_BODY) // 2 * 2], dtype="<i2")
    peak = int(np.abs(samples.astype(np.int32)).max())
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    assert peak >= 32000  # essentially 0.0 dBFS
    assert rms > 5000  # her own speech measures an RMS of ~800


def test_a_float_wave_container_reinterpreted_as_int16_is_full_scale():
    """The container header is what is loud: b"RIFF" reads as 18770, 17990..."""
    payload = (np.sin(np.linspace(0, 80 * np.pi, 4000)) * 0.5).astype("<f4").tobytes()
    body = _wav(44100, payload, channels=1, width=4, fmt_tag=3)
    raw = body[: len(body) // 2 * 2]
    assert _peak(raw) >= 32000


# --------------------------------------------------------------------------
# The fix: a body that is not PCM we can render is refused, never played
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body, name",
    [
        (MP3_BODY, "MP3"),
        (b"ID3\x04\x00\x00" + b"\x00" * 64, "ID3"),
        (b"OggS\x00\x02" + b"\x00" * 64, "Ogg"),
        (b"fLaC\x00\x00" + b"\x00" * 64, "FLAC"),
        (b"\x1f\x8b\x08\x00" + b"\x00" * 64, "gzip"),
        (b"<!DOCTYPE html><html>nope</html>", "HTML"),
        (b'{"error": "quota exceeded", "detail": "x"}', "JSON"),
    ],
)
def test_a_non_pcm_body_is_refused_and_nothing_is_played(body, name):
    """The whole point: no bytes reach the device, so no burst is possible."""
    player = _run_to_idle(body)
    assert player.written == [], f"{name} body was played: {len(player.written)} blocks"
    assert player.opened == []


def test_the_refusal_names_the_format_the_error_contract_is_fish_error():
    fmt = _PcmStreamFormat()
    with pytest.raises(FishUnsupportedAudioFormatError) as excinfo:
        fmt.feed(MP3_BODY)
    assert "MP3" in str(excinfo.value)
    from claire.output.fish_client import FishError

    assert isinstance(excinfo.value, FishError)


def test_an_unparseable_riff_container_is_refused_not_blasted():
    """Past the sniff budget a RIFF we cannot open must fail, not play raw."""
    big = (np.sin(np.linspace(0, 800 * np.pi, 40000)) * 0.5).astype("<f4").tobytes()
    body = _wav(44100, big, channels=1, width=4, fmt_tag=3)
    assert len(body) > MAX_WAV_HEADER_BYTES
    player = _run_to_idle(body)
    assert player.written == []
    assert player.opened == []


def test_a_short_riff_prefix_is_held_not_played_as_pcm():
    """3 bytes cannot be told apart from b"RIF"; they must wait, not sound."""
    fmt = _PcmStreamFormat()
    assert fmt.feed(b"\x77") == b""
    assert fmt.feed(b"\x77\x77") == b""
    assert fmt.feed(b"\x77\x77") == b"\x77\x77\x77\x77"  # 4th byte decides
    assert fmt.sample_rate == fish_tts.DEFAULT_SAMPLE_RATE


# --------------------------------------------------------------------------
# Normal audio must be untouched
# --------------------------------------------------------------------------

def test_a_16_bit_mono_wav_still_plays_byte_for_byte():
    payload = b"\x11\x22" * 500
    player = _run_to_idle(_wav(24000, payload))
    assert player.opened == [24000]
    assert b"".join(player.written) == payload
    assert _peak(payload) == 0x2211


def test_headerless_pcm_still_plays_at_the_default_rate():
    payload = b"\x33" * 4096
    player = _run_to_idle(payload, chunk_size=1000)
    assert player.opened == [44100]
    assert b"".join(player.written) == payload


# --------------------------------------------------------------------------
# A declared layout is converted, never reinterpreted
# --------------------------------------------------------------------------

def test_stereo_16_bit_is_folded_to_mono_at_the_declared_rate():
    left = np.full(2000, 3000, dtype="<i2")
    right = np.full(2000, 1000, dtype="<i2")
    interleaved = np.empty(4000, dtype="<i2")
    interleaved[0::2] = left
    interleaved[1::2] = right
    player = _run_to_idle(_wav(24000, interleaved.tobytes(), channels=2, width=2))
    played = b"".join(player.written)
    assert player.opened == [24000]
    assert len(played) == 2000 * PLAYBACK_FRAME_BYTES  # half of the stereo bytes
    assert _peak(played) == 2000  # (3000 + 1000) / 2, not 3000 or 1000


@pytest.mark.parametrize(
    "width, payload, expected_peak",
    [
        # 8-bit WAV is unsigned with a 128 bias; 0 -> -32768, 255 -> 32512.
        (1, bytes([0, 255]), 32768),
        # 24-bit keeps the top 16 bits.
        (3, b"\xff\xff\x7f", 32767),
        # 32-bit keeps the top 16 bits.
        (4, struct.pack("<i", 0x7FFF0000), 32767),
    ],
)
def test_each_declared_width_is_scaled_into_range(width, payload, expected_peak):
    fmt = _PcmStreamFormat()
    out = fmt.feed(_wav(44100, payload, channels=1, width=width))
    assert _peak(out) == expected_peak
    assert len(out) == len(payload) // width * PLAYBACK_FRAME_BYTES


def test_eight_bit_pcm_converts_to_the_top_of_the_range():
    fmt = _PcmStreamFormat()
    out = fmt.feed(_wav(44100, bytes([0, 255, 128]), channels=1, width=1))
    samples = np.frombuffer(out, dtype="<i2")
    assert list(samples) == [-32768, 32512, 0]


def test_twenty_four_bit_pcm_keeps_the_top_sixteen_bits():
    fmt = _PcmStreamFormat()
    raw = b"".join(
        int(v & 0xFFFFFF).to_bytes(3, "little")
        for v in [0x7FFFFF, -0x800000, 0x000100]
    )
    out = fmt.feed(_wav(44100, raw, channels=1, width=3))
    samples = np.frombuffer(out, dtype="<i2")
    assert list(samples) == [32767, -32768, 1]


def test_a_declared_stride_is_never_reinterpreted_as_a_shorter_one():
    """32-bit PCM read as 16-bit is a 0x0000/0x7FFF alternation - a loud buzz."""
    fmt = _PcmStreamFormat()
    payload = (np.linspace(-1.0, 1.0, 500) * 0.5 * (2 ** 31)).astype("<i4")
    out = fmt.feed(_wav(44100, payload.tobytes(), channels=1, width=4))
    assert len(out) == payload.size * PLAYBACK_FRAME_BYTES
    assert _peak(out) == int(np.abs(payload).max()) >> 16


# --------------------------------------------------------------------------
# Frame alignment: a partial sample must not be dropped silently
# --------------------------------------------------------------------------

def test_odd_length_chunks_are_aligned_not_dropped():
    payload = b"\x11\x22" * 500
    fmt = _PcmStreamFormat()
    emitted = []
    for start in range(0, 44 + len(payload), 7):
        emitted.append(fmt.feed((_wav(44100, payload))[start:start + 7]))
    assert fmt.flush_tail() == b""
    assert b"".join(emitted) == payload


def test_a_dangling_half_sample_at_end_of_sentence_is_dropped_not_played():
    fmt = _PcmStreamFormat()
    assert fmt.feed(b"\x11\x22\x33\x44\x55") == b"\x11\x22\x33\x44"
    assert fmt.flush_tail() == b""


def test_the_engine_never_hands_out_a_partial_frame():
    payload = b"\x11" * 999  # odd total: one byte can never complete a frame
    player = _run_to_idle(payload)
    assert player.written, "the even prefix must still play"
    assert all(len(block) % PLAYBACK_FRAME_BYTES == 0 for block in player.written)


# --------------------------------------------------------------------------
# The device player: whole frames, and a peak backstop
# --------------------------------------------------------------------------

class _FakeRawStream:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.blocks: list[bytes] = []
        self.started = False
        self.aborted = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def write(self, data) -> None:
        # The real sounddevice.RawStream.write raises for a partial frame.
        _, samplesize = 2, 2
        if len(data) % samplesize:
            raise ValueError("len(data) not divisible by samplesize")
        self.blocks.append(bytes(data))

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True


def _install_fake_sounddevice(monkeypatch):
    created: list[_FakeRawStream] = []

    def _RawOutputStream(*args, **kwargs):
        stream = _FakeRawStream(**kwargs)
        created.append(stream)
        return stream

    fake = types.ModuleType("sounddevice")
    fake.RawOutputStream = _RawOutputStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    return created


def test_the_stream_is_opened_as_the_format_the_engine_produces(monkeypatch):
    created = _install_fake_sounddevice(monkeypatch)
    player = fish_tts._SoundDevicePlayer()
    player.open(44100)
    assert created[0].kwargs["dtype"] == "int16"
    assert created[0].kwargs["channels"] == PLAYBACK_FRAME_BYTES // 2 == 1
    assert created[0].kwargs["samplerate"] == 44100
    player.close()


def test_the_player_never_writes_a_partial_frame(monkeypatch):
    created = _install_fake_sounddevice(monkeypatch)
    player = fish_tts._SoundDevicePlayer()
    player.open(44100)
    for start in range(0, 201, 7):  # every block is an odd length
        player.write(b"\x11" * min(7, 201 - start))
    assert created[0].blocks, "the aligned prefix must reach the device"
    assert all(len(block) % PLAYBACK_FRAME_BYTES == 0 for block in created[0].blocks)
    player.close()


def test_the_player_carries_the_remainder_instead_of_losing_it(monkeypatch):
    created = _install_fake_sounddevice(monkeypatch)
    player = fish_tts._SoundDevicePlayer()
    player.open(44100)
    player.write(b"\x01\x02\x03")
    assert created[0].blocks == [b"\x01\x02"]
    player.write(b"\x04")
    assert created[0].blocks == [b"\x01\x02", b"\x03\x04"]
    player.close()


def test_limit_peak_leaves_normal_speech_byte_for_byte():
    quiet = (np.sin(np.linspace(0, 40 * np.pi, 2000)) * 5000).astype("<i2").tobytes()
    assert _limit_peak(quiet) == quiet


def test_limit_peak_scales_a_full_scale_block_down_to_the_ceiling():
    loud = np.full(1000, 32767, dtype="<i2").tobytes()
    limited = _limit_peak(loud)
    assert _peak(limited) <= fish_tts._PEAK_CEILING
    assert len(limited) == len(loud)
    # it is a scale, not a truncation: a quiet block stays proportional
    assert _peak(limited) > fish_tts._PEAK_CEILING // 2


def test_limit_peak_handles_the_most_negative_sample():
    loud = np.full(64, -32768, dtype="<i2").tobytes()
    assert _peak(_limit_peak(loud)) <= fish_tts._PEAK_CEILING
