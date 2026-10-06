"""Shared microphone selection, format negotiation and mono conversion."""

from contextlib import nullcontext

from ..debug import debug_log
from .audio_lock import portaudio_lock


def resolve_input_device(sounddevice, voice_device, devices=None):
    """Resolve one input index for every phase of a capture session."""
    selected = str(voice_device if voice_device is not None else '').strip()
    if not selected or selected.lower() in ('default', 'system'):
        try:
            info = sounddevice.query_devices(kind='input')
            if info['index'] < 0 or info['max_input_channels'] <= 0:
                raise ValueError('Default device has no microphone input')
        except Exception as exc:
            raise ValueError(
                'System default microphone unavailable. Choose an available input '
                'in Claire Settings or set a default microphone in system Settings.'
            ) from exc
        debug_log(f"Resolved default microphone: {info.get('name', 'Unknown')} (index {info['index']})", 'audio')
        return {'device': info['index']}
    try:
        return {'device': int(selected)}
    except ValueError:
        pass

    if devices is None:
        devices = sounddevice.query_devices()
    for index, device in enumerate(devices):
        name = str(device.get('name') or '')
        if (device.get('max_input_channels') or 0) > 0 and selected.lower() in name.lower():
            return {'device': index}
    raise ValueError('Selected microphone not found. Choose an available input in Settings.')


def resolve_output_device(sounddevice, voice_output_device, devices=None):
    """Resolve the playback device for speech, mirroring the input resolver.

    Accepts an explicit index, a substring of the device name, or a system
    default marker. The default case deliberately returns the *named* ALSA
    ``default`` PCM rather than letting PortAudio pick:

    PortAudio numbers ALSA's hardware devices and takes the first one it likes
    as "default". On a machine whose speakers are a USB device held by
    PipeWire, that landed on the HDMI output of the graphics card - the monitor
    - so Claire spoke into a display with no speakers while the sink the user
    was actually listening to never appeared in the device list at all. ALSA's
    ``default`` follows the system's real routing (PipeWire/PulseAudio), which
    is the device the user chose in their own settings.
    """
    selected = str(voice_output_device if voice_output_device is not None else '').strip()
    if not selected or selected.lower() in ('default', 'system'):
        return {'device': 'default'}
    try:
        return {'device': int(selected)}
    except ValueError:
        pass

    if devices is None:
        devices = sounddevice.query_devices()
    for index, device in enumerate(devices):
        name = str(device.get('name') or '')
        if (device.get('max_output_channels') or 0) > 0 and selected.lower() in name.lower():
            return {'device': index}
    raise ValueError('Selected speaker not found. Choose an available output in Settings.')


def _is_input_format_error(exc):
    """Distinguish unsupported capture formats from access and device failures."""
    code = exc.args[1] if len(exc.args) > 1 else None
    message = str(exc).lower()
    return code in (-9998, -9997) or any(part in message for part in (
        'invalid number of channels', 'invalid channel count',
        'invalid sample rate', 'paerrorcode -9998', 'paerrorcode -9997',
    ))


def open_input_stream(sounddevice, sample_rate, frame_ms, device_kwargs, *,
                      callback=None, serialise=True, log_category='voice', fallback_rate=None):
    """Try bounded channel/rate formats on one selected input device."""
    candidates = [(sample_rate, 1)]
    first_error = None
    for rate, channels in candidates:
        try:
            with portaudio_lock if serialise else nullcontext():
                stream = sounddevice.InputStream(
                    samplerate=rate, channels=channels, dtype='float32',
                    blocksize=max(1, int(rate * frame_ms / 1000)),
                    callback=callback, **device_kwargs,
                )
            debug_log(f'Input format accepted: {rate} Hz, {channels} channel(s)', log_category)
            return stream, rate, channels
        except Exception as exc:
            if not _is_input_format_error(exc):
                raise
            if first_error is None:
                first_error = exc
            debug_log(f'Input format rejected: {rate} Hz, {channels} channel(s): {exc}', log_category)
            if len(candidates) == 1:
                try:
                    info = (sounddevice.query_devices(device_kwargs['device'])
                            if 'device' in device_kwargs else sounddevice.query_devices(kind='input'))
                    native_rate = int(info.get('default_samplerate', sample_rate))
                    max_channels = int(info.get('max_input_channels', 1))
                except Exception:
                    raise first_error
                rates = list(dict.fromkeys(
                    rate for rate in (sample_rate, native_rate, fallback_rate)
                    if rate is not None and rate > 0
                ))
                counts = list(dict.fromkeys(
                    count for count in (1, 2, max_channels) if 0 < count <= max_channels
                ))
                candidates.extend((r, c) for c in counts for r in rates if (r, c) != candidates[0])
    raise first_error


def mono_capture(indata):
    """Retain microphone signal from every captured channel."""
    return indata.mean(axis=1) if indata.ndim > 1 else indata.flatten()
