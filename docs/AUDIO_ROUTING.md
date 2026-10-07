# Audio routing on this machine

Claire was, for the whole life of this build, **inaudible and then unintelligible**.
Neither fault was in her code. Both are recorded here because they are
environment facts a reinstall would lose.

## 1. She played to a monitor with no speakers

`python-sounddevice` (PortAudio) is built here with only the **ALSA** and **OSS**
host APIs — there is no PulseAudio backend. ALSA's unconfigured `default` PCM
resolves to raw hardware, and PortAudio numbered the NVIDIA HDMI output first, so
Claire opened `HDA NVidia: DELL S2721HGF (hw:0,3)`: the monitor.

The speakers are a **C-Media USB Audio Device**, and PipeWire holds it
exclusively — which is why that device never appeared in the ALSA list at all and
Claire could not reach it even by index.

Fix: `~/.asoundrc` sends ALSA's `default` through the installed
`libasound_module_pcm_pipewire.so`:

```
pcm.!default { type pipewire }
ctl.!default { type pipewire }
```

Claire then plays to the same sink as every other application. `voice_output_device`
in the config overrides this per-install; unset means the system default.

## 2. Her speech came out as noise

`fish_tts.py` understands RIFF/WAVE and headerless PCM, and treats **anything
else** as raw PCM. Fish Audio's default response is **MP3**. So her MP3 bytes
were written straight into an `int16` stream: loud, confident, unintelligible
noise — with every layer reporting success.

Fix: `fish_client.py` requests `"format": "wav"`. Verified against the live API:
default → `\xff\xfb` (MP3); `format=wav` → `RIFF`, 1ch 44100Hz 16-bit, which is
exactly what the player already expects.

Useful to know: the generated PCM is clean (peak 4439 of 32767 across 372,736
samples, no outliers). **A remaining intermittent loud burst is therefore a
device-level artefact, not bad audio** — it is not fixed, and the honest next
step is to record the sink monitor with `parec` while it happens rather than
theorise about it.
