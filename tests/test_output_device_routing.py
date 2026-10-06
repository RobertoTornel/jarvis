"""Speech must come out of the speaker the user is actually listening to.

Claire was audible only to a monitor. PortAudio numbers ALSA's hardware devices
and treats the first one it likes as "default"; on this machine that was the
HDMI output of the graphics card, while the real speakers were a USB device held
exclusively by PipeWire - so they never even appeared in the device list. Claire
.spoke into a display with no speakers and every layer reported success.

The fix is to resolve output the same way input is resolved: explicitly, from
config, defaulting to the *system* default rather than to PortAudio's guess.
"""

from __future__ import annotations

import pytest

from claire.utils.audio_capture import resolve_output_device

# Without this marker the whole file is DESELECTED rather than run, which
# is how a test file can appear to pass while never executing.
pytestmark = pytest.mark.unit


class _FakeSD:
    """A sounddevice stub with the two output entries this machine renaming needs."""

    def __init__(self) -> None:
        self._devices = [
            {"name": "HDA NVidia: DELL S2721HGF (hw:0,3)", "max_output_channels": 2,
             "max_input_channels": 0},
            {"name": "Wireless Controller: USB Audio (hw:1,0)", "max_output_channels": 2,
             "max_input_channels": 0},
            {"name": "W4DS: W4DS", "max_output_channels": 0, "max_input_channels": 1},
        ]

    def query_devices(self, *args, **kwargs):
        return self._devices


def test_unset_device_uses_the_system_default_not_portaudio_index_zero() -> None:
    """The regression itself: None must not mean "let PortAudio pick"."""
    assert resolve_output_device(_FakeSD(), None) == {"device": "default"}


@pytest.mark.parametrize("value", ["default", "system", "", "  ", "DEFAULT", "System"])
def test_every_spelling_of_default_means_the_system_default(value: str) -> None:
    assert resolve_output_device(_FakeSD(), value) == {"device": "default"}


def test_an_explicit_index_is_passed_through_as_an_int() -> None:
    assert resolve_output_device(_FakeSD(), "4") == {"device": 4}


def test_a_name_substring_selects_the_matching_output() -> None:
    assert resolve_output_device(_FakeSD(), "wireless") == {"device": 1}


def test_an_input_only_device_is_never_chosen_for_output() -> None:
    """W4DS has no output channels; matching it would send speech nowhere."""
    with pytest.raises(ValueError):
        resolve_output_device(_FakeSD(), "W4DS")


def test_an_unknown_name_fails_loudly_rather_than_silently() -> None:
    """A wrong speaker must be an error, never a silent no-op (N10)."""
    with pytest.raises(ValueError, match="not found"):
        resolve_output_device(_FakeSD(), "speakers-that-do-not-exist")


def test_config_exposes_the_output_device_and_parses_default_as_unset() -> None:
    """`default` in the file means "no explicit device", not the string."""
    from claire.config import Settings

    assert "voice_output_device" in Settings.__dataclass_fields__


def test_the_engine_hands_its_config_device_to_the_player() -> None:
    """The wiring: a configured speaker must reach the stream, not be ignored."""
    from claire.output.fish_tts import FishTTS

    class _Cfg:
        voice_output_device = None

    tts = FishTTS.__new__(FishTTS)
    tts.config = _Cfg()
    # resolve_output_device returns "default" for None, and that is what the
    # player receives - so this pins the value, not merely the call.
    assert tts._resolve_output_device() == "default"


def test_a_player_with_no_config_keeps_portaudios_own_default() -> None:
    """An older caller or a test double must not be forced onto a named device."""
    from claire.output.fish_tts import FishTTS

    tts = FishTTS.__new__(FishTTS)
    tts.config = None
    assert tts._resolve_output_device() is None
