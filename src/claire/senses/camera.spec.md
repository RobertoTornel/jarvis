# Camera Sense Specification (R-VISION-1)

`src/claire/senses/camera.py` gives Claire a camera sense: it can say whether a
camera exists, take one picture with one, and ask a vision model what is in that
picture. It is the second sense in `claire.senses` alongside screen reading
([screen.spec.md](screen.spec.md)).

## Goals

1. **Honest about absence.** A laptop with no webcam is an expected machine, not
   a fault. "There is no camera" is a *capability verdict* Claire speaks
   plainly — never an exception the daemon trips over, never a silent empty
   result, never a retry loop.
2. **Never fabricate sight.** A description is only ever produced from a real,
   decoded frame that a configured model actually answered about. Everything
   else returns an explicit *not seen* result. A confident spoken description of
   an image that was never captured is the worst failure this module could
   produce, so the type system makes it unrepresentable.
3. **One route, already owned.** Vision goes through the project's existing
   `claire.llm` backend (OmniRouter by default). This module owns **no HTTP
   client** and no second copy of the base URL, key or model selection.
4. **Named failures with the fix.** Every missing device, missing package,
   missing key and refused route names what is wrong and gives the exact
   command, environment variable or thing to plug in.

## Measured facts on this machine (the tested default)

| Fact | Consequence |
| --- | --- |
| `/dev/video*` does not exist (laptop, no webcam) | `detect_cameras() == []`; `capture_frame()` raises `CameraNotFoundError` |
| OmniRouter at `http://127.0.0.1:20129/v1`, provider `omniroute` | vision rides the existing `claire.llm` route; model `cfg.llm_chat_model` |
| `OMNIROUTE_API_KEY` supplies the key | absent key raises `VisionUnavailableError` naming that variable |
| Pillow 10.4.0 available | frame decode/encode and image validation live in Pillow |
| No `v4l2-ctl`, no `ffmpeg` assumed | metadata comes from sysfs; capture uses in-process V4L2 `read()` I/O |

The "no camera" path is therefore not a fallback branch in the tests: it is the
default branch, exercised on the real machine (`test_this_machine_has_no_camera_and_empty_is_the_tested_default`).

## Public surface

```python
from claire.senses.camera import (
    # detection + verdict
    detect_cameras, describe_availability, diagnose,
    CameraDevice, CameraAvailability,
    # capture
    capture_frame, CapturedFrame, camera_lock,
    # description seam
    describe_image, describe_image_or_raise, ImageDescription, ImageDescriber,
    CloudVisionDescriber, STATUS_SEEN, STATUS_NOT_SEEN,
    # named errors
    CameraError, CameraNotFoundError, CameraCaptureError,
    FrameNotFoundError, NotAnImageError, MissingDependencyError,
    VisionUnavailableError, VisionNoAnswerError, VisionRequestError,
)
```

### Detection

```python
detect_cameras(dev_root="/dev", *, sysfs_root="/sys/class/video4linux",
               read_metadata=True) -> list[CameraDevice]
```

* Globs `<dev_root>/video*`, keeps only names matching `video<digits>` (so
  `video-helper`, `video`, `video1.bak` are ignored), sorts by numeric index,
  and annotates each device with the V4L2 name and driver read from sysfs.
* **Why sysfs and not `v4l2-ctl`:** sysfs is one small text file per device; it
  needs no subprocess, the tool is not installed here, and — most importantly —
  it never *opens* the device. Opening `/dev/video0` to ask its name would fight
  a running viewer for exclusive access, so detection stays read-only.
* Metadata failures are swallowed to `""`; detection itself never fails.
* `_video_device_paths(dev_root)` is the single seam tests patch to simulate a
  machine with no camera. Passing `dev_root=<tmp>` additionally exercises the
  whole scan without touching real hardware.

### Availability verdict

```python
describe_availability(dev_root=..., sysfs_root=..., read_metadata=True) -> CameraAvailability
diagnose(...) -> CameraAvailability            # thin alias for doctor-style call sites
```

`CameraAvailability` is frozen and carries `available`, `devices`, `scanned`,
`reason`, `actions`, plus `render()` (multi-line verdict) and
`spoken_summary()` (one spoken sentence). With no camera, `reason` says nothing
matched `/dev/video*` and calls it a missing capability, and `actions` carries
the four `NO_CAMERA_FIXES`:

1. plug in a USB webcam and check with `ls -l /dev/video*`;
2. if it is plugged in but missing, `sudo modprobe uvcvideo` and `dmesg | tail -n 20`;
3. if the device exists but is refused, `sudo usermod -aG video $USER` then re-login;
4. or use the phone — send a photo, no camera needed here.

No exception is raised anywhere on this path.

### Capture

```python
capture_frame(device=None, *, dev_root="/dev", sysfs_root=...,
              out_dir=None, width=640, height=480,
              warmup_frames=2, timeout_sec=5.0) -> pathlib.Path
```

1. `detect_cameras()`; **no devices → `CameraNotFoundError`**, naming the glob
   that was scanned, that this is not a crash, and the same four fixes.
2. Resolve `device` (`None`/blank → first, `int`/digit string → index, else path
   or case-insensitive name substring). An unknown request raises
   `CameraNotFoundError` with `requested` set and the attached paths listed.
3. Under `camera_lock`, open the device and read one frame.
4. Decode to RGB with Pillow, write `<out_dir>/claire_frame_<timestamp>_<hex>.jpg`
   (quality 90). `out_dir` defaults to `$XDG_CACHE_HOME/claire/frames`
   (`~/.cache/claire/frames`).
5. Return the `Path` that was actually written — the caller describes *that*
   file, so nothing can be described that was not captured.

**Capture mechanics.** V4L2 single-planar capture with `read()` I/O, not
mmap/streaming: `VIDIOC_S_FMT` (208-byte `struct v4l2_format`) negotiates MJPEG
first, then YUYV (`EBUSY`/format rejections are collected and reported
together). MJPEG frames are self-delimiting (SOI `FFD8` … EOI `FFD9`) and YUYV
frames are exactly `width*height*2` bytes, so no buffer queue is needed. The
loop discards `warmup_frames` frames so auto-exposure has settled, enforces the
whole exchange with a monotonically-checked deadline via `select`, and caps the
accumulation buffer at 16 MiB. `CapturedFrame` refuses to be empty;
`_extract_frame` (partial buffers), `_yuyv_to_ycbcr` (4:2:2 chroma sharing) and
`_frame_to_image` (decode) are unit-tested without hardware.

### Locking discipline

`camera_lock = threading.RLock()` is the camera analogue of
`claire.utils.audio_lock.portaudio_lock`, and the analogy is real: a V4L2
capture node is an exclusive device (a second opener gets `EBUSY`) and two
threads racing `VIDIOC_S_FMT` + `read()` on the same node corrupt each other's
frames exactly the way concurrent PortAudio stream lifecycle calls corrupt audio
(#462/#401/#422). Every open, ioctl, read and close therefore runs inside
`camera_lock` — and only that exchange.

It is deliberately a **separate** lock from `portaudio_lock`: taking the PortAudio
lock to grab a still frame would block the microphone for a capture that has
nothing to do with audio, and vice versa. It is also never held across the model
call in `describe_image`, which is network-bound and may take tens of seconds;
holding a device lock across a cloud request would stall every other camera user
for no reason. `test_capture_frame_saves_a_real_jpeg_from_a_fake_driver_under_the_lock`
asserts the lock is held inside the fake driver read; the absence of a broader
lock is the point, not an omission.

No equivalent discipline is needed for the description seam: it touches no
device, only an immutable file path, and the backend it calls is already
thread-safe for independent requests.

### Image description seam

```python
describe_image(image_path, question="", *, describer=None, config=None,
               model="", timeout_sec=60.0) -> ImageDescription
describe_image_or_raise(image_path, question="", **kwargs) -> ImageDescription
```

**Preconditions raise, before any describer is touched:**

* the path does not exist or is not a file → `FrameNotFoundError`;
* the bytes are not a decodable image → `NotAnImageError`;
* Pillow is missing → `MissingDependencyError` (names `pip install pillow`).

This is what makes "never describe an image that was not captured mechanical":
there is no code path that reaches a model without a real frame on disk.

**After that, nothing raises and nothing is invented:**

| Situation | Result |
| --- | --- |
| describer injected and answers | `seen=True`, `description=<model's words>` |
| describer injected, returns blank/None | `seen=False`, `error=VisionNoAnswerError` |
| describer injected, raises anything | `seen=False`, named error classified (see below) |
| describer is not callable | `seen=False`, `VisionUnavailableError` |
| no describer and no usable route | `seen=False`, `VisionUnavailableError` (e.g. absent key) |
| backend answers blank | `seen=False`, `VisionNoAnswerError` |

`ImageDescription` enforces the invariant in `__post_init__`: `seen=True` with an
empty description raises `ValueError`, and `seen=False` without a named error
raises `ValueError`. `spoken_summary()` therefore either quotes the model's own
words ("Here is what I can see: …") or says "I could not see that image." —
never a guess. `raise_if_not_seen()` (used by `describe_image_or_raise`) turns a
not-seen result back into its named exception for callers that prefer raising.

**The default describer reuses `claire.llm`.** `CloudVisionDescriber(config)`
validates configuration *before* any image is sent, then builds the standard
multimodal message and calls the backend:

```python
messages = [
    {"role": "system", "content": VISION_SYSTEM_PROMPT},          # "never invent…"
    {"role": "user", "content": [
        {"type": "text", "text": question or DEFAULT_VISION_QUESTION},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}},
    ]},
]
text = extract_text_from_response(backend.chat(cfg.llm_chat_model, messages, timeout_sec=…))
```

* The MIME type comes from Pillow's sniffed format, so a PNG named `.jpg` is
  sent as PNG.
* Model: `model` argument, else `config.llm_chat_model` — the field the project's
  config load always resolves (OmniRouter's default is
  `cmd/deepseek/deepseek-v4.1-flash`). Empty model → `VisionUnavailableError`
  telling the user to set `llm_chat_model`.
* Key: `config.llm_api_key`, else `OMNIROUTE_API_KEY` from the environment —
  the same precedence as `claire.config`. The route requires a key when the
  provider is `omniroute`, when the base URL is OmniRouter's, or when the host is
  not loopback; a local OpenAI-compatible server without a key remains usable.
  No key → `VisionUnavailableError` whose fix is
  `export OMNIROUTE_API_KEY=<your key>`.
* Transport failures are classified into named errors with a fix: HTTP 401/403 →
  key problem; other HTTP status → `VisionRequestError`; timeout → "did not
  answer before the timeout"; `OSError`/`ConnectionError` → "could not be
  reached" with the `curl -s <base>/models` check; anything else → named
  `VisionRequestError` carrying the exception type and message.

**Injectable by construction.** `describer` accepts an object with
`describe(image_path, question)` or a plain callable with that signature, and
`config`/`backend` are injected rather than imported at the call site, so tests
never make a network call — the tests additionally patch `get_llm_backend` to a
tripwire and assert it is never reached on the absent-key path.

## Named errors and their remedies

| Error | Raised when | Remedy carried |
| --- | --- | --- |
| `CameraNotFoundError` | no camera, or the requested one is absent | `scanned` (the glob), `requested`, `available`, the four no-camera fixes; `CameraCaptureError` |
| `CameraCaptureError` | camera exists, no frame delivered/decodable/writable | `v4l2-ctl --all -d <path>` (`sudo apt-get install -y v4l-utils`), close other viewers, try another size/device |
| `FrameNotFoundError` | description asked for a path that is not a file | capture a frame first, or point at an existing image |
| `NotAnImageError` | bytes are not a decodable image | point at a real JPEG/PNG or capture one |
| `MissingDependencyError` | Pillow missing | `python -m pip install pillow` (mapped from the import name) |
| `VisionUnavailableError` | no model, no key, uncallable describer, 401/403 | set `llm_chat_model`, or `export OMNIROUTE_API_KEY=…` |
| `VisionNoAnswerError` | model answered with nothing | check the model is loaded: `curl -s <base>/models` |
| `VisionRequestError` | transport/HTTP failure | reachability check with `curl -s <base>/models` |

Every one derives from `CameraError(RuntimeError)` and exposes `remedy` (plus
`cause` where an underlying exception exists), so a caller can always answer
"what do I do about it?" without parsing prose.

## Tests

`tests/test_senses_camera.py` (40 unit tests, `pytestmark = pytest.mark.unit`)
covers: the real empty scan on this machine; scan filtering, ordering and sysfs
metadata; the availability verdict and its advice; the named no-camera capture
error and that no device is opened when the scan is empty; device selection by
index/name and the unknown-device error; an unopenable device named error with
the video-group fix; a fake-driver capture that writes a real 64×48 JPEG
**and asserts `camera_lock` is held**; the V4L2 helper
functions; the precondition raises (missing path, directory, non-image) with a
spy proving the describer was never called; injected object/callable describers;
blank, failing and uncallable describers producing explicit not-seen results;
absent-key and config-load failures producing named results with the
`OMNIROUTE_API_KEY` fix and a `get_llm_backend` tripwire proving no network;
the data-URL message shape and `claire.llm` reuse; and the
`ImageDescription` invariants.

No test uses a real camera, opens a real device, or touches the network.

## Non-goals

* **No video, no preview, no streaming.** One still frame per request; mmap
  streaming would add a buffer queue and latency for no benefit to a voice
  assistant that describes one picture.
* **No `ffmpeg`/`v4l2-ctl`/`fswebcam` shell-outs.** One in-process path is
  testable and has no external tool to be absent.
* **No phone integration here.** The phone photo arrives through an existing
  channel; this module only points at it in the no-camera advice.
* **No image storage policy.** Frames land in the cache directory; retention and
  redaction belong to a later privacy pass.
* **No continuous monitoring.** Claire looks when asked, not continuously.
