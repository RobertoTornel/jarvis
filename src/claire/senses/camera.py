"""Claire's camera sense: detect a camera, capture a frame, describe it.

Spec: ``src/claire/senses/camera.spec.md`` (R-VISION-1).

The public surface is small and deliberate:

* :func:`detect_cameras` — what video devices exist, scanning ``/dev/video*``
  and reading V4L2 metadata from sysfs (no subprocess, no opened device).
* :func:`describe_availability` / :func:`diagnose` — a structured, human
  readable verdict. *No camera is not an error*: it is a capability Claire
  reports plainly, together with what the user can do about it.
* :func:`capture_frame` — one saved image path, or a named
  :class:`CameraNotFoundError` / :class:`CameraCaptureError` carrying the
  exact command to fix the problem.
* :func:`describe_image` — the image-description seam. It talks to the
  *project's* configured LLM backend (``claire.llm``), never a second HTTP
  client, and it is injectable so tests never touch the network.

Two invariants are enforced mechanically rather than by good intentions:

1. **No fabrication.** :class:`ImageDescription` refuses to exist in the
   ``seen`` state without a non-empty description
   (:meth:`ImageDescription.__post_init__`), and a describer that is absent,
   unreachable, faulty or silent produces an explicit *not seen* result
   carrying a named error — never an invented picture description. A spoken
   hallucination about an image nobody captured is the worst failure this
   module could produce, so it is made unrepresentable.
2. **No silent empty results.** Every missing dependency, absent device and
   refused route raises, or returns, a named error with an actionable fix:
   the install command, the environment variable, or what to plug in.

Measured facts this module is built around (the honest path is the tested
default):

* there is no ``/dev/video*`` on this machine (a laptop with no webcam), so
  :func:`detect_cameras` returns ``[]`` here and ``capture_frame`` raises
  :class:`CameraNotFoundError`;
* OmniRouter at ``http://127.0.0.1:20129/v1`` needs ``OMNIROUTE_API_KEY``; an
  absent key raises :class:`VisionUnavailableError` naming that variable.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import glob
import io
import os
import re
import select
import struct
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..config import (
    OMNIROUTE_BASE_URL,
    OMNIROUTE_KEY_ENV,
    OMNIROUTE_PROVIDER_ALIAS,
    load_settings,
)
from ..debug import debug_log
from ..llm import extract_text_from_response, get_llm_backend
from ..llm.errors import is_timeout_error

__all__ = [
    "CameraAvailability",
    "CameraCaptureError",
    "CameraDevice",
    "CameraError",
    "CameraNotFoundError",
    "CloudVisionDescriber",
    "CapturedFrame",
    "FrameNotFoundError",
    "ImageDescription",
    "ImageDescriber",
    "MissingDependencyError",
    "NotAnImageError",
    "STATUS_NOT_SEEN",
    "STATUS_SEEN",
    "VisionNoAnswerError",
    "VisionRequestError",
    "VisionUnavailableError",
    "camera_lock",
    "capture_frame",
    "describe_availability",
    "describe_image",
    "describe_image_or_raise",
    "detect_cameras",
    "diagnose",
]


# --------------------------------------------------------------------------- #
# Where devices and metadata live
# --------------------------------------------------------------------------- #

#: Where Linux exposes video capture devices.
DEV_ROOT = "/dev"
#: The glob that finds them. Reported verbatim in "what was scanned" messages.
VIDEO_DEVICE_PATTERN = "video*"
#: sysfs is the cheapest V4L2 metadata source: one small text file per device
#: (``/sys/class/video4linux/video0/name``). It needs no subprocess and, unlike
#: ``v4l2-ctl``, does not open the device — opening it would fight a running
#: viewer for exclusive access. ``v4l2-ctl`` is deliberately not shelled out to.
SYSFS_VIDEO4LINUX_ROOT = "/sys/class/video4linux"
_VIDEO_NAME_RE = re.compile(r"^video(\d+)$")


# --------------------------------------------------------------------------- #
# Named errors. Each one names what is missing and the command that fixes it.
# --------------------------------------------------------------------------- #


class CameraError(RuntimeError):
    """Base class for every camera-sense failure.

    Subclasses always carry an actionable remedy: the install command, the
    environment variable to export, or what to plug in.
    """


#: What the user can do about a machine with no camera. Kept as data so the
#: availability verdict, the capture error and the tests all quote the same
#: advice instead of drifting apart.
NO_CAMERA_FIXES: Tuple[str, ...] = (
    "Plug in a USB webcam; it normally appears as /dev/video0 within a second "
    "or two. Check with: ls -l /dev/video*",
    "If a webcam is already plugged in but /dev/video* is empty, load the "
    "kernel driver: sudo modprobe uvcvideo (then inspect it with: dmesg | tail -n 20)",
    "If /dev/video* exists but Claire cannot open it, grant your user access to "
    "the video group: sudo usermod -aG video $USER (then log out and back in)",
    "Or use the phone instead of a webcam: open Claire's phone page and send a "
    "photo; it arrives as the most recent photo and needs no camera here",
)


class CameraNotFoundError(CameraError):
    """No camera exists (or the requested one is not attached).

    States plainly what was scanned and what to do. This is a *capability*
    report, not a crash: a laptop with no webcam is an expected machine.
    """

    def __init__(
        self,
        scanned: Sequence[str] = (),
        requested: Any = None,
        available: Sequence[str] = (),
        fixes: Sequence[str] = (),
    ) -> None:
        self.scanned = [str(item) for item in scanned]
        self.requested = None if requested is None else str(requested)
        self.available = [str(item) for item in available]
        self.fixes = [str(item) for item in fixes] or list(NO_CAMERA_FIXES)
        self.remedy = self.fixes[0]

        lines: List[str] = []
        if self.requested and self.available:
            lines.append(
                f"Camera {self.requested!r} is not attached. Attached cameras: "
                + ", ".join(self.available)
                + "."
            )
        elif self.requested:
            lines.append(
                f"Camera {self.requested!r} is not attached, and no camera was found."
            )
        else:
            lines.append("No camera found on this machine.")
        if self.scanned:
            lines.append("Scanned: " + ", ".join(self.scanned) + ".")
        lines.append(
            "This is not a crash: it means Claire has no camera to look through."
        )
        lines.append("Fix (any one of these):")
        lines.extend(f"    - {fix}" for fix in self.fixes)
        super().__init__("\n".join(lines))


class CameraCaptureError(CameraError):
    """A camera exists but a frame could not be read from it."""

    def __init__(
        self,
        device_path: Any = "",
        reason: str = "",
        fixes: Sequence[str] = (),
        cause: Optional[BaseException] = None,
    ) -> None:
        self.device_path = str(device_path)
        self.reason = reason or "the driver did not deliver a usable frame"
        self.fixes = [str(item) for item in fixes] or [
            f"Inspect the device: v4l2-ctl --all -d {self.device_path} "
            "(install with: sudo apt-get install -y v4l-utils)",
            "Close any other program using the camera; most V4L2 devices are "
            "exclusive and a running viewer will refuse Claire access",
            "Try another capture resolution, or another device from: ls -l /dev/video*",
        ]
        self.remedy = self.fixes[0]
        self.cause = cause
        message = (
            f"Could not capture a frame from {self.device_path or 'the camera'}: "
            f"{self.reason}.\n"
            "Fix (any one of these):\n"
            + "\n".join(f"    - {fix}" for fix in self.fixes)
        )
        super().__init__(message)


class FrameNotFoundError(CameraError):
    """A frame path was named but no such image file exists."""

    def __init__(self, image_path: Any) -> None:
        self.image_path = str(image_path)
        self.remedy = (
            "Capture a frame first, e.g. capture_frame(), or point Claire at an "
            "existing image file."
        )
        super().__init__(
            f"No frame to describe: {self.image_path!r} does not exist (or is not a "
            "file), so nothing was seen and nothing will be described.\n"
            f"Fix: {self.remedy}"
        )


class NotAnImageError(CameraError):
    """The path exists but its bytes are not a decodable image."""

    def __init__(self, image_path: Any, reason: str = "", cause: Optional[BaseException] = None) -> None:
        self.image_path = str(image_path)
        self.reason = reason or "the file is not a decodable image"
        self.remedy = (
            "Point Claire at a real image (JPEG/PNG), or capture one with "
            "capture_frame()."
        )
        self.cause = cause
        super().__init__(
            f"Not an image: {self.image_path!r} — {self.reason}. Nothing was seen, "
            f"so nothing will be described.\nFix: {self.remedy}"
        )


class MissingDependencyError(CameraError):
    """A Python package the camera sense needs is not installed."""

    #: Import names that differ from their pip package name.
    PIP_NAMES: Dict[str, str] = {
        "PIL": "pillow",
        "cv2": "opencv-python",
        "pytesseract": "pytesseract",
        "requests": "requests",
        "numpy": "numpy",
    }

    def __init__(
        self,
        module: str,
        package: str = "",
        command: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        self.module = module
        self.package = package or self.PIP_NAMES.get(module, module.lower())
        self.command = command or f"{sys.executable} -m pip install {self.package}"
        self.cause = cause
        self.remedy = self.command
        super().__init__(
            f"Missing dependency: {module!r} is not importable, so this camera "
            f"operation cannot run.\nFix: {self.command}"
        )


class VisionUnavailableError(CameraError):
    """The vision route is not usable: no model configured, or no API key.

    Raised *before* any image is sent, so an absent key can never turn into an
    empty description.
    """

    def __init__(
        self,
        message: str,
        remedy: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        self.remedy = remedy or (
            f"Export the router key: export {OMNIROUTE_KEY_ENV}=<your key>"
        )
        self.cause = cause
        super().__init__(f"{message}\nFix: {self.remedy}")


class VisionNoAnswerError(CameraError):
    """The model was asked and answered with nothing (or nothing usable)."""

    def __init__(
        self,
        model: str = "",
        base_url: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.cause = cause
        self.remedy = (
            f"Check the vision model is loaded at {base_url or OMNIROUTE_BASE_URL} "
            f"(model {model!r}); confirm with: "
            f"curl -s {base_url or OMNIROUTE_BASE_URL}/models"
        )
        super().__init__(
            "The vision model returned an empty answer, so Claire did not see the "
            f"image and will not pretend otherwise.\nFix: {self.remedy}"
        )


class VisionRequestError(CameraError):
    """The image was sent but the vision request failed in transport."""

    def __init__(
        self,
        reason: str,
        base_url: str = "",
        remedy: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        self.reason = reason
        self.base_url = base_url
        self.cause = cause
        self.remedy = remedy or (
            f"Check the router is running at {base_url or OMNIROUTE_BASE_URL} "
            f"with: curl -s {base_url or OMNIROUTE_BASE_URL}/models"
        )
        super().__init__(
            f"Vision request failed: {reason}\nFix: {self.remedy}"
        )


# --------------------------------------------------------------------------- #
# Device detection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CameraDevice:
    """One ``/dev/videoN`` capture device."""

    path: str
    index: int
    name: str = ""
    driver: str = ""

    @property
    def label(self) -> str:
        if self.name:
            return f"{self.name} ({self.path})"
        return self.path


def _video_device_paths(dev_root: Union[str, os.PathLike[str]] = DEV_ROOT) -> List[str]:
    """Return every ``/dev/video*`` path, sorted.

    This is the single seam tests patch to simulate a machine without a
    camera: monkeypatch this function (or pass ``dev_root``) and nothing
    downstream will look at real hardware.
    """
    pattern = os.path.join(str(dev_root), VIDEO_DEVICE_PATTERN)
    return sorted(glob.glob(pattern))


def _read_text_file(path: Union[str, os.PathLike[str]]) -> str:
    """Best-effort small-file read; metadata failures never break detection."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _metadata_for(path: str, sysfs_root: Union[str, os.PathLike[str]]) -> Tuple[str, str]:
    """Return ``(name, driver)`` for a device, read from sysfs when available."""
    stem = os.path.basename(path)
    name = _read_text_file(os.path.join(str(sysfs_root), stem, "name"))
    driver = ""
    driver_link = os.path.join(str(sysfs_root), stem, "device", "driver")
    try:
        driver = os.path.basename(os.path.realpath(driver_link))
    except OSError:  # pragma: no cover - realpath almost never raises here
        driver = ""
    if driver in ("", os.sep):
        driver = ""
    return name, driver


def detect_cameras(
    dev_root: Union[str, os.PathLike[str]] = DEV_ROOT,
    *,
    sysfs_root: Union[str, os.PathLike[str]] = SYSFS_VIDEO4LINUX_ROOT,
    read_metadata: bool = True,
) -> List[CameraDevice]:
    """Detect video capture devices.

    Scans ``<dev_root>/video*`` and, when ``read_metadata`` is true, annotates
    each device with the V4L2 name and driver read cheaply from sysfs. On this
    machine (no ``/dev/video*``) the result is an empty list — that is the
    tested default, not a failure.
    """
    devices: List[CameraDevice] = []
    for path in _video_device_paths(dev_root):
        match = _VIDEO_NAME_RE.match(os.path.basename(path))
        if match is None:
            continue
        name, driver = ("", "")
        if read_metadata:
            name, driver = _metadata_for(path, sysfs_root)
        devices.append(
            CameraDevice(path=path, index=int(match.group(1)), name=name, driver=driver)
        )
    devices.sort(key=lambda device: device.index)
    debug_log(
        f"Camera scan of {os.path.join(str(dev_root), VIDEO_DEVICE_PATTERN)}: "
        f"{len(devices)} device(s) found",
        "senses",
    )
    return devices


@dataclass(frozen=True)
class CameraAvailability:
    """Structured, human-readable verdict on the camera capability."""

    available: bool
    devices: Tuple[CameraDevice, ...] = ()
    scanned: Tuple[str, ...] = ()
    reason: str = ""
    actions: Tuple[str, ...] = ()

    def render(self) -> str:
        """Multi-line verdict for logs, the settings UI or a doctor screen."""
        lines = [f"Camera: {'available' if self.available else 'not available'}"]
        if self.devices:
            for device in self.devices:
                lines.append(f"  - {device.label}")
        if self.reason:
            lines.append(self.reason)
        if self.scanned:
            lines.append("Scanned: " + ", ".join(self.scanned))
        if not self.available and self.actions:
            lines.append("What you can do:")
            lines.extend(f"  - {action}" for action in self.actions)
        return "\n".join(lines)

    def spoken_summary(self) -> str:
        """One spoken sentence: plain capability, no apology, no invention."""
        if self.available:
            names = ", ".join(device.label for device in self.devices)
            return f"Yes, I have a camera: {names}."
        return (
            "I do not have a camera on this machine, so I cannot take a picture. "
            "You can plug in a USB webcam, or send me a photo from your phone."
        )


def describe_availability(
    dev_root: Union[str, os.PathLike[str]] = DEV_ROOT,
    *,
    sysfs_root: Union[str, os.PathLike[str]] = SYSFS_VIDEO4LINUX_ROOT,
    read_metadata: bool = True,
) -> CameraAvailability:
    """Report whether a camera exists and what the user can do about it.

    A missing camera is a capability verdict, never an exception and never a
    crash — that is the whole point of separating this from
    :func:`capture_frame`.
    """
    devices = detect_cameras(
        dev_root, sysfs_root=sysfs_root, read_metadata=read_metadata
    )
    scanned = (os.path.join(str(dev_root), VIDEO_DEVICE_PATTERN),)
    if devices:
        count = len(devices)
        plural = "cameras" if count != 1 else "camera"
        return CameraAvailability(
            available=True,
            devices=tuple(devices),
            scanned=scanned,
            reason=f"{count} {plural} detected and ready to capture from.",
        )
    return CameraAvailability(
        available=False,
        devices=(),
        scanned=scanned,
        reason=(
            f"No camera found: nothing matching {scanned[0]} exists, so Claire "
            "cannot take a picture. That is a missing capability, not a fault."
        ),
        actions=tuple(NO_CAMERA_FIXES),
    )


def diagnose(
    dev_root: Union[str, os.PathLike[str]] = DEV_ROOT,
    *,
    sysfs_root: Union[str, os.PathLike[str]] = SYSFS_VIDEO4LINUX_ROOT,
    read_metadata: bool = True,
) -> CameraAvailability:
    """Alias of :func:`describe_availability` for doctor-style call sites."""
    return describe_availability(
        dev_root, sysfs_root=sysfs_root, read_metadata=read_metadata
    )


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #

#: Process-wide serialisation of camera open/negotiate/read/close.
#:
#: The audio side has exactly this concern: ``claire.utils.audio_lock``
#: serialises PortAudio stream lifecycle calls because two threads opening the
#: same device corrupt each other. A V4L2 capture device is the same kind of
#: exclusive resource — a second opener gets ``EBUSY``, and two threads racing
#: format negotiation can read each other's frames. So every open, ioctl,
#: read and close runs inside ``camera_lock``. A separate lock from
#: ``portaudio_lock`` on purpose: capturing a frame must not block the
#: microphone, and the two devices are independent. The lock is held only
#: around the device exchange — never across the model call in
#: :func:`describe_image`, which is network-bound and may take seconds.
camera_lock = threading.RLock()

#: V4L2 ioctls (single-planar capture, read() I/O).
_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
_VIDIOC_S_FMT = 0xC0D05605  # _IOWR('V', 5, struct v4l2_format), sizeof == 208
_V4L2_FORMAT_BUFFER_SIZE = 208
_V4L2_PIX_FMT_MJPEG = struct.unpack("<I", b"MJPG")[0]
_V4L2_PIX_FMT_YUYV = struct.unpack("<I", b"YUYV")[0]
_V4L2_PIX_FMT_NAMES = {_V4L2_PIX_FMT_MJPEG: "MJPG", _V4L2_PIX_FMT_YUYV: "YUYV"}
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_READ_CHUNK = 256 * 1024


@dataclass(frozen=True)
class CapturedFrame:
    """Raw bytes handed back by the driver, before decoding."""

    data: bytes
    pixel_format: str
    width: int
    height: int

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("CapturedFrame cannot be empty; no frame was captured")


def _require_pillow():
    """Import Pillow or raise a named error with the install command."""
    try:
        from PIL import Image  # noqa: WPS433 - deliberate lazy import
    except ImportError as exc:  # pragma: no cover - Pillow is a listed dependency
        raise MissingDependencyError("PIL", "pillow", cause=exc) from exc
    return Image


def _negotiate_format(
    fd: int, width: int, height: int, device_path: str = ""
) -> Tuple[int, int, str, int]:
    """Pick a pixel format Claire can decode; return (width, height, fmt, size)."""
    failures: List[str] = []
    for pixel_format in (_V4L2_PIX_FMT_MJPEG, _V4L2_PIX_FMT_YUYV):
        buffer = bytearray(_V4L2_FORMAT_BUFFER_SIZE)
        struct.pack_into("<I", buffer, 0, _V4L2_BUF_TYPE_VIDEO_CAPTURE)
        # width, height, pixelformat, field (0 == V4L2_FIELD_ANY)
        struct.pack_into("<IIII", buffer, 4, width, height, pixel_format, 0)
        try:
            fcntl.ioctl(fd, _VIDIOC_S_FMT, buffer, True)
        except OSError as exc:
            failures.append(f"{_V4L2_PIX_FMT_NAMES[pixel_format]}: {exc}")
            continue
        out_width, out_height, out_format, _ = struct.unpack_from("<IIII", buffer, 4)
        _bytesperline, sizeimage = struct.unpack_from("<II", buffer, 16)
        if out_width <= 0 or out_height <= 0:
            failures.append(
                f"{_V4L2_PIX_FMT_NAMES[pixel_format]}: driver reported "
                f"{out_width}x{out_height}"
            )
            continue
        if out_format not in _V4L2_PIX_FMT_NAMES:
            failures.append(
                f"{_V4L2_PIX_FMT_NAMES[pixel_format]}: driver switched to unknown "
                f"format {out_format:#010x}"
            )
            continue
        return out_width, out_height, _V4L2_PIX_FMT_NAMES[out_format], int(sizeimage)
    raise CameraCaptureError(
        device_path,
        "the driver refused every pixel format Claire can decode ("
        + "; ".join(failures)
        + ")",
        fixes=[
            "List what the camera supports: v4l2-ctl --list-formats-ext -d /dev/video0 "
            "(install with: sudo apt-get install -y v4l-utils)",
            "Try another attached device from: ls -l /dev/video*",
            "If another program is already using the camera, close it and retry",
        ],
    )


def _extract_frame(
    data: bytes, pixel_format: str, width: int, height: int
) -> Optional[bytes]:
    """Pull one complete frame out of the accumulating read buffer."""
    if pixel_format == "MJPG":
        start = data.find(b"\xff\xd8")
        end = data.find(b"\xff\xd9", start) if start >= 0 else -1
        if start < 0 or end < 0:
            return None
        return data[start : end + 2]
    if pixel_format == "YUYV":
        needed = width * height * 2
        if len(data) < needed:
            return None
        return data[:needed]
    return None


def _capture_v4l2_read(
    device_path: Union[str, os.PathLike[str]],
    *,
    width: int = 640,
    height: int = 480,
    warmup_frames: int = 2,
    timeout_sec: float = 5.0,
) -> CapturedFrame:
    """Read one frame over V4L2 read() I/O.

    Read I/O rather than mmap/streaming keeps the ioctl surface to a single
    ``VIDIOC_S_FMT`` and one blocking-read loop; MJPEG frames are
    self-delimiting and YUYV frames are a fixed size, so no buffer queue is
    needed. The caller holds :data:`camera_lock` while this runs.
    """
    path = str(device_path)
    if warmup_frames < 0:
        warmup_frames = 0
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as exc:
        raise CameraCaptureError(
            path,
            f"the device could not be opened: {exc}",
            fixes=[
                f"Check the device exists and is readable: ls -l {path}",
                "If this says 'Permission denied', join the video group: "
                "sudo usermod -aG video $USER (then log out and back in)",
                "If this says 'Device or resource busy', close the other program "
                "using the camera",
            ],
            cause=exc,
        ) from exc
    try:
        out_width, out_height, pixel_format, _sizeimage = _negotiate_format(
            fd, int(width), int(height), path
        )
        buffer = bytearray()
        frames_seen = 0
        deadline = time.monotonic() + max(float(timeout_sec), 0.1)
        while frames_seen <= warmup_frames:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CameraCaptureError(
                    path,
                    f"no complete {pixel_format} frame arrived within "
                    f"{float(timeout_sec):g}s "
                    f"(read {len(buffer)} byte(s) of a partial frame)",
                )
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except OSError as exc:
                raise CameraCaptureError(path, f"the device became unusable: {exc}", cause=exc) from exc
            if not ready:
                continue
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except BlockingIOError:
                continue
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    continue
                raise CameraCaptureError(
                    path,
                    f"reading the device failed: {exc}",
                    fixes=[
                        "If this says 'Device or resource busy', close the other "
                        "program using the camera",
                        f"Check permissions: ls -l {path} (you may need to be in the "
                        "video group: sudo usermod -aG video $USER)",
                        "Verify the device works at all: v4l2-ctl --all -d "
                        f"{path} (sudo apt-get install -y v4l-utils)",
                    ],
                    cause=exc,
                ) from exc
            if not chunk:
                continue
            buffer.extend(chunk)
            frame = _extract_frame(bytes(buffer), pixel_format, out_width, out_height)
            if frame is None:
                if len(buffer) > _MAX_FRAME_BYTES:
                    del buffer[:-_READ_CHUNK]
                continue
            frames_seen += 1
            buffer.clear()
            if frames_seen > warmup_frames:
                debug_log(
                    f"Captured a {out_width}x{out_height} {pixel_format} frame from "
                    f"{path} after {frames_seen} frame(s)",
                    "senses",
                )
                return CapturedFrame(
                    data=frame,
                    pixel_format=pixel_format,
                    width=out_width,
                    height=out_height,
                )
        raise CameraCaptureError(  # pragma: no cover - loop returns or raises
            path, "the capture loop ended without a frame"
        )
    finally:
        os.close(fd)


def _yuyv_to_ycbcr(data: bytes, width: int, height: int) -> bytes:
    """Expand YUYV 4:2:2 bytes into the 3-bytes-per-pixel YCbCr Pillow decodes.

    Each ``Y0 U Y1 V`` group becomes ``Y0 U V Y1 U V``: the chroma of a pair is
    shared by both pixels, which is exactly what 4:2:2 subsampling means.
    """
    out = bytearray(width * height * 3)
    span = min(len(data) // 4, (width // 2) * height)
    position = 0
    for group in range(span):
        base = group * 4
        y0, u, y1, v = data[base], data[base + 1], data[base + 2], data[base + 3]
        out[position] = y0
        out[position + 1] = u
        out[position + 2] = v
        out[position + 3] = y1
        out[position + 4] = u
        out[position + 5] = v
        position += 6
    return bytes(out)


def _frame_to_image(frame: CapturedFrame, device_path: str = ""):
    """Decode a :class:`CapturedFrame` into an RGB Pillow image."""
    Image = _require_pillow()
    try:
        if frame.pixel_format == "MJPG":
            image = Image.open(io.BytesIO(frame.data))
            image.load()
            return image.convert("RGB")
        if frame.pixel_format == "YUYV":
            image = Image.frombytes(
                "YCbCr",
                (frame.width, frame.height),
                _yuyv_to_ycbcr(frame.data, frame.width, frame.height),
            )
            return image.convert("RGB")
    except Exception as exc:
        raise CameraCaptureError(
            device_path,
            f"the frame the driver returned could not be decoded ({type(exc).__name__}: {exc})",
            cause=exc,
        ) from exc
    raise CameraCaptureError(
        device_path,
        f"unsupported pixel format {frame.pixel_format!r}",
        fixes=[
            "Report this device's formats: v4l2-ctl --list-formats-ext -d "
            f"{device_path or '/dev/video0'}",
        ],
    )


def _default_out_dir() -> Path:
    """Where captured frames are written when the caller does not choose."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(base) / "claire" / "frames"


def _select_device(
    devices: Sequence[CameraDevice],
    device: Any,
    scanned: Sequence[str],
) -> CameraDevice:
    """Resolve ``device`` (index, path or name substring) against what exists."""
    if device is None or (isinstance(device, str) and not device.strip()):
        return devices[0]
    available = [item.path for item in devices]
    if isinstance(device, int) or (isinstance(device, str) and device.strip().isdigit()):
        index = int(device)
        for item in devices:
            if item.index == index:
                return item
        raise CameraNotFoundError(
            scanned=scanned, requested=index, available=available
        )
    wanted = str(device).strip()
    for item in devices:
        if item.path == wanted:
            return item
    lowered = wanted.casefold()
    for item in devices:
        if lowered in item.name.casefold() or lowered == os.path.basename(item.path).casefold():
            return item
    raise CameraNotFoundError(scanned=scanned, requested=wanted, available=available)


def capture_frame(
    device: Any = None,
    *,
    dev_root: Union[str, os.PathLike[str]] = DEV_ROOT,
    sysfs_root: Union[str, os.PathLike[str]] = SYSFS_VIDEO4LINUX_ROOT,
    out_dir: Optional[Union[str, os.PathLike[str]]] = None,
    width: int = 640,
    height: int = 480,
    warmup_frames: int = 2,
    timeout_sec: float = 5.0,
) -> Path:
    """Capture one frame and return the path of the saved image.

    Raises :class:`CameraNotFoundError` when there is no camera (naming what was
    scanned and what to do) and :class:`CameraCaptureError` when a camera
    exists but will not deliver a frame. The device's open/read/close exchange
    runs under :data:`camera_lock`.
    """
    devices = detect_cameras(dev_root, sysfs_root=sysfs_root)
    scanned = [os.path.join(str(dev_root), VIDEO_DEVICE_PATTERN)]
    if not devices:
        raise CameraNotFoundError(scanned=scanned)
    chosen = _select_device(devices, device, scanned)
    with camera_lock:
        frame = _capture_v4l2_read(
            chosen.path,
            width=width,
            height=height,
            warmup_frames=warmup_frames,
            timeout_sec=timeout_sec,
        )
    image = _frame_to_image(frame, chosen.path)
    target_dir = Path(out_dir) if out_dir is not None else _default_out_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CameraCaptureError(
            chosen.path,
            f"the frame directory {str(target_dir)!r} could not be created: {exc}",
            cause=exc,
        ) from exc
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = target_dir / f"claire_frame_{stamp}_{uuid.uuid4().hex[:6]}.jpg"
    try:
        image.save(path, "JPEG", quality=90)
    except OSError as exc:
        raise CameraCaptureError(
            chosen.path,
            f"the captured frame could not be written to {str(path)!r}: {exc}",
            fixes=[
                "Check the directory is writable: "
                f"ls -ld {str(target_dir)!r}",
                "Pick another directory with capture_frame(out_dir=...)",
            ],
            cause=exc,
        ) from exc
    debug_log(f"Saved camera frame to {path}", "senses")
    return path


# --------------------------------------------------------------------------- #
# Image description seam
# --------------------------------------------------------------------------- #

STATUS_SEEN = "seen"
STATUS_NOT_SEEN = "not_seen"

#: Quoted inside the "no model configured" fix; the real default lives in
#: ``claire.config.OMNIROUTE_DEFAULT_CHAT_MODEL``.
OMNIROUTE_DEFAULT_CHAT_MODEL_HINT = "cmd/deepseek/deepseek-v4.1-flash"

DEFAULT_VISION_QUESTION = (
    "Describe what is in this image briefly and concretely. If you cannot tell "
    "what it shows, say so instead of guessing."
)

VISION_SYSTEM_PROMPT = (
    "You are Claire's camera sense. Describe only what is actually visible in "
    "the image. Never invent objects, people, text or places that are not "
    "visible. If the image is too dark, blurry or ambiguous to identify, say "
    "that plainly. Answer in one or two spoken sentences."
)


class ImageDescriber:
    """Protocol for anything that can turn an image path into a description.

    Implementations are ``describe(image_path, question) -> str``. A plain
    callable of the same shape is accepted too; tests inject either and no
    network call is ever made from this module under test.
    """

    def describe(self, image_path: Union[str, os.PathLike[str]], question: str = "") -> str:  # pragma: no cover - protocol
        raise NotImplementedError


@dataclass(frozen=True)
class ImageDescription:
    """The result of asking a vision model about one frame.

    ``seen`` is True only when a real description came back. The invariant is
    enforced here rather than trusted: a ``seen`` result without a description
    cannot be constructed, so no caller can accidentally speak a confident
    sentence about an image nobody looked at.
    """

    seen: bool
    description: str
    image_path: str
    question: str = ""
    status: str = STATUS_SEEN
    error: Optional[BaseException] = None
    remedy: str = ""

    def __post_init__(self) -> None:
        if self.seen and not (self.description or "").strip():
            raise ValueError(
                "Refusing to report 'seen' with no description: a vision result "
                "must carry the model's own words or be explicitly not seen."
            )
        if not self.seen and self.error is None:
            raise ValueError(
                "A not-seen image result must carry the named error that explains why."
            )

    @classmethod
    def not_seen(
        cls,
        image_path: Union[str, os.PathLike[str]],
        question: str,
        error: BaseException,
    ) -> "ImageDescription":
        """Build the explicit 'I did not see it' result."""
        return cls(
            seen=False,
            description="",
            image_path=str(image_path),
            question=question,
            status=STATUS_NOT_SEEN,
            error=error,
            remedy=str(getattr(error, "remedy", "") or ""),
        )

    def render(self) -> str:
        """Human-readable verdict for logs or a doctor screen."""
        if self.seen:
            return f"Seen ({self.image_path}): {self.description}"
        return (
            f"Not seen ({self.image_path}): {self.error}\n"
            f"Fix: {self.remedy or 'nothing to fix automatically'}"
        )

    def spoken_summary(self) -> str:
        """What Claire may say out loud. Never invents a description."""
        if self.seen:
            return f"Here is what I can see: {self.description}"
        detail = str(self.error) if self.error else "the image was not seen"
        return f"I could not see that image. {detail}"

    def raise_if_not_seen(self) -> "ImageDescription":
        """Turn an explicit not-seen result back into its named error."""
        if not self.seen:
            assert self.error is not None  # guaranteed by __post_init__
            raise self.error
        return self


def _resolve_api_key(config: Any) -> str:
    """The configured key, falling back to the environment variable."""
    key = getattr(config, "llm_api_key", "") or ""
    key = str(key).strip()
    if key:
        return key
    return os.environ.get(OMNIROUTE_KEY_ENV, "").strip()


def _same_url(left: str, right: str) -> bool:
    return str(left or "").strip().rstrip("/").casefold() == str(right or "").strip().rstrip("/").casefold()


def _route_requires_key(config: Any) -> bool:
    """True when the configured route is one that must authenticate.

    OmniRouter always needs ``OMNIROUTE_API_KEY``; any non-loopback endpoint is
    assumed to need credentials too. A local OpenAI-compatible server without a
    key stays usable.
    """
    provider = str(getattr(config, "llm_provider", "") or "").strip().lower()
    base_url = str(getattr(config, "llm_base_url", "") or "").strip()
    if provider == OMNIROUTE_PROVIDER_ALIAS or _same_url(base_url, OMNIROUTE_BASE_URL):
        return True
    host = (urllib.parse.urlsplit(base_url).hostname or "") if base_url else ""
    return host not in ("", "127.0.0.1", "localhost", "::1", "0.0.0.0")


_MIME_BY_PIL_FORMAT: Dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}
_MIME_BY_SUFFIX: Dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _image_mime(path: Path) -> str:
    """Return the frame's MIME type, refusing anything Pillow cannot decode."""
    Image = _require_pillow()
    try:
        with Image.open(path) as image:
            pillow_format = (image.format or "").upper()
    except FileNotFoundError as exc:
        raise FrameNotFoundError(path) from exc
    except OSError as exc:
        raise NotAnImageError(path, cause=exc) from exc
    mime = _MIME_BY_PIL_FORMAT.get(pillow_format)
    if mime:
        return mime
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower())
    if mime:
        return mime
    raise NotAnImageError(
        path,
        reason=f"unrecognised image format {pillow_format or 'unknown'!r}",
    )


class CloudVisionDescriber:
    """Describe a frame through the project's configured cloud LLM.

    This deliberately owns **no HTTP client**: it builds the multimodal message
    and calls ``claire.llm``'s backend, which already resolves OmniRouter
    (provider ``omniroute``, base URL ``http://127.0.0.1:20129/v1``, key from
    ``OMNIROUTE_API_KEY``) exactly as every other Claire call site does.

    All configuration problems raise :class:`VisionUnavailableError` *before*
    the image is sent, so a missing key can never surface as an empty string
    that someone might mistake for "nothing is in the picture".
    """

    def __init__(
        self,
        config: Any,
        *,
        backend: Any = None,
        model: str = "",
        timeout_sec: float = 60.0,
    ) -> None:
        self._config = config
        self._base_url = str(getattr(config, "llm_base_url", "") or OMNIROUTE_BASE_URL)
        self._model = str(model or getattr(config, "llm_chat_model", "") or "").strip()
        if not self._model:
            raise VisionUnavailableError(
                "No vision model is configured, so an image cannot be described.",
                remedy=(
                    'Set "llm_chat_model" in Claire\'s config.json (for OmniRouter '
                    f"the default is {OMNIROUTE_DEFAULT_CHAT_MODEL_HINT!r})"
                ),
            )
        if _route_requires_key(config) and not _resolve_api_key(config):
            raise VisionUnavailableError(
                f"The vision route at {self._base_url} needs an API key and none is "
                f"configured (config value empty and {OMNIROUTE_KEY_ENV} unset).",
                remedy=(
                    f"export {OMNIROUTE_KEY_ENV}=<your OmniRouter key>  "
                    "# then restart Claire; the key is read from the environment, "
                    "never from config.json"
                ),
            )
        self._backend = backend if backend is not None else get_llm_backend(config)
        self._timeout_sec = float(timeout_sec)

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    def build_messages(self, image_path: Union[str, os.PathLike[str]], question: str = "") -> List[Dict[str, Any]]:
        """Build the multimodal chat messages; split out so tests can inspect."""
        path = Path(image_path)
        mime = _image_mime(path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise FrameNotFoundError(path) from exc
        encoded = base64.b64encode(raw).decode("ascii")
        prompt = (question or "").strip() or DEFAULT_VISION_QUESTION
        return [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"},
                    },
                ],
            },
        ]

    def describe(self, image_path: Union[str, os.PathLike[str]], question: str = "") -> str:
        """Ask the model about the frame; never return an empty description."""
        messages = self.build_messages(image_path, question)
        response = self._backend.chat(
            self._model, messages, timeout_sec=self._timeout_sec
        )
        text = extract_text_from_response(response) if isinstance(response, dict) else None
        if not (isinstance(text, str) and text.strip()):
            raise VisionNoAnswerError(model=self._model, base_url=self._base_url)
        return text.strip()


def _invoke_describer(
    describer: Any, image_path: Path, question: str
) -> Any:
    """Call an injected describer: object with ``describe`` or plain callable."""
    method = getattr(describer, "describe", None)
    if callable(method):
        return method(image_path, question)
    if callable(describer):
        return describer(image_path, question)
    raise VisionUnavailableError(
        f"The configured describer {describer!r} is not callable.",
        remedy="Pass an object with a describe(image_path, question) method, or a "
        "function with the same signature.",
    )


def _classify_vision_failure(exc: BaseException, base_url: str = "") -> CameraError:
    """Wrap an arbitrary failure in a named error that says what to do."""
    if isinstance(exc, CameraError):
        return exc
    url = base_url or OMNIROUTE_BASE_URL
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return VisionUnavailableError(
            f"The vision route at {url} rejected the request with HTTP {status} "
            "(bad or missing credentials).",
            remedy=f"Re-export a valid key: export {OMNIROUTE_KEY_ENV}=<your key>",
            cause=exc,
        )
    if status is not None:
        return VisionRequestError(
            f"the router answered HTTP {status} ({type(exc).__name__})",
            base_url=url,
            cause=exc,
        )
    if is_timeout_error(exc):
        return VisionRequestError(
            "the vision model did not answer before the timeout",
            base_url=url,
            remedy=(
                f"Check the router at {url} is reachable and the model is loaded: "
                f"curl -s {url}/models"
            ),
            cause=exc,
        )
    if isinstance(exc, OSError):
        return VisionRequestError(
            f"the vision route could not be reached ({type(exc).__name__}: {exc})",
            base_url=url,
            remedy=(
                f"Start OmniRouter (or the configured LLM server) at {url}, then "
                f"verify with: curl -s {url}/models"
            ),
            cause=exc,
        )
    return VisionRequestError(
        f"{type(exc).__name__}: {exc}",
        base_url=url,
        cause=exc,
    )


def describe_image(
    image_path: Union[str, os.PathLike[str]],
    question: str = "",
    *,
    describer: Any = None,
    config: Any = None,
    model: str = "",
    timeout_sec: float = 60.0,
) -> ImageDescription:
    """Describe an already-captured frame, or say explicitly that it was not seen.

    Preconditions raise: a path that is missing, is not a file, or is not a
    decodable image raises :class:`FrameNotFoundError` /
    :class:`NotAnImageError` **before** any describer is touched, which is what
    makes "never describe something that was not captured" mechanical.

    Everything after that is failure-tolerant but never fabricating: an absent
    key, an unreachable router, a faulty describer or an empty answer all return
    an :class:`ImageDescription` with ``seen=False`` carrying a named error and
    its remedy. Use :func:`describe_image_or_raise` when the caller would rather
    have the exception.
    """
    path = Path(image_path)
    if not path.is_file():
        raise FrameNotFoundError(path)
    _image_mime(path)  # Named error if these bytes are not an image.

    effective_config = config
    if describer is None:
        try:
            if effective_config is None:
                effective_config = load_settings()
            describer = CloudVisionDescriber(
                effective_config, model=model, timeout_sec=timeout_sec
            )
        except CameraError as exc:
            return ImageDescription.not_seen(path, question, exc)
        except Exception as exc:  # config load failure — still a named report
            return ImageDescription.not_seen(
                path,
                question,
                VisionUnavailableError(
                    f"The vision route could not be configured: {type(exc).__name__}: {exc}",
                    remedy=(
                        f"Check Claire's config.json and export {OMNIROUTE_KEY_ENV}; "
                        "then retry."
                    ),
                    cause=exc,
                ),
            )

    try:
        answer = _invoke_describer(describer, path, question)
    except CameraError as exc:
        return ImageDescription.not_seen(path, question, exc)
    except Exception as exc:
        base_url = str(getattr(effective_config, "llm_base_url", "") or "")
        return ImageDescription.not_seen(
            path, question, _classify_vision_failure(exc, base_url)
        )

    if not isinstance(answer, str) or not answer.strip():
        base_url = str(getattr(effective_config, "llm_base_url", "") or "")
        model_name = str(getattr(effective_config, "llm_chat_model", "") or model or "")
        return ImageDescription.not_seen(
            path,
            question,
            VisionNoAnswerError(model=model_name, base_url=base_url),
        )

    description = answer.strip()
    debug_log(
        f"Vision model described {path} ({len(description)} chars)", "senses"
    )
    return ImageDescription(
        seen=True,
        description=description,
        image_path=str(path),
        question=question,
        status=STATUS_SEEN,
    )


def describe_image_or_raise(
    image_path: Union[str, os.PathLike[str]],
    question: str = "",
    **kwargs: Any,
) -> ImageDescription:
    """Like :func:`describe_image`, but raises the named error on not-seen."""
    return describe_image(image_path, question, **kwargs).raise_if_not_seen()
