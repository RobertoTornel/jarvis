# Speech output specification

Synthesis runs through one of three engines: `piper`, `chatterbox` or `fish`
(`SUPPORTED_TTS_ENGINES`, `src/claire/output/tts.py:1038`). The configured
default is `fish` (declared at `src/claire/config.py:630`, read at `:986`), so the cloud path below
is what a fresh install takes.

Piper and Chatterbox synthesise locally. Piper loads a local ONNX voice and its
JSON configuration; Chatterbox loads local model weights. Neither sends reply
text, microphone audio or any local file to a synthesis service.

Fish is a cloud engine and a voice-cloning service: `fish_voice_id` names a
cloned voice held by Fish Audio, not a local model. Each sentence of a reply is
posted to `https://api.fish.audio/v1/tts`
(`src/claire/output/fish_client.py:68`), authenticated with one key from
`fish_key_file`. The request body carries the spoken reply text, the configured
`fish_voice_id`, the model id and the latency setting (`fish_model`,
`fish_latency`; `fish_client.py:556`–`:574`). This code uploads no audio and no
local file — no reference audio, no microphone capture and no conversation
history are sent (`docs/CLOUD_POLICY.md`). Selecting `piper` or `chatterbox`
keeps synthesis on the machine.

## Startup availability

Both speech backends load their local model before accepting speech work. A
model or dependency failure reports its reason and disables speech output for
that engine instance. No worker or queued speech is started for an unavailable
backend. The daemon reports availability after startup, and continues initialising
the listener and text chat when speech output is unavailable. Saved settings are
unchanged; restarting Claire creates a fresh engine that can load a repaired model.

## Piper voice downloads

Missing voice files are downloaded from the configured public model-file source.
Existing model and configuration files are retained. Each missing file is written
to a temporary sibling and published only after the response stream completes.

Connection failures, connect/read timeouts, interrupted response bodies and HTTP
429 responses share a bounded retry budget per file. Backoff increases between
attempts. Each retry starts the incomplete file from the beginning; completed
files are retained if downloading the other file fails. Responses are closed on
success and failure. Certificate/TLS errors and other HTTP failures are not
retried. Exhausted retries report failure and remove unpublished temporary files.
Locked temporary files are reported without crashing startup or publishing them.

Progress includes transferred bytes and transfer rate, with a percentage when
the response supplies a total size. Completion is reported only after publication.
Download diagnostics use `debug_log` without recording user speech or reply text.

## Per-utterance callbacks

The queued-work engines (`PiperTTS`, `ChatterboxTTS`) carry each utterance as a
`_SpeechItem` — the text plus *its own* `completion_callback` and
`duration_callback`. `FishTTS` has always carried them per item; the other two
used to hold them on the engine as shared attributes, which is only correct
while at most one utterance is ever in flight.

That assumption no longer holds: Claire speaks a short acknowledgement
immediately and enqueues the reply behind it
(`listening.spec.md`, "Spoken Acknowledgement"). With shared attributes the
acknowledgement's completion invoked the *reply's* callback and then cleared
it, so the reply finished without ever running the callback that frees the hot
window. The reply's duration callback was likewise spent on the wrong
utterance, weakening echo detection.

Contract: `speak()` enqueues one `_SpeechItem`; the worker invokes the callbacks
of the item it is currently speaking, and only if that item was not
interrupted. A plain `str` left in the queue (the stop sentinel, or a producer
that predates the type) is coerced to a callback-less `_SpeechItem`, so
`""` still terminates the worker as before.
