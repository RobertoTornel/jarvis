"""Tests for the camera sense (R-VISION-1).

No real camera, no network, ever:

* device scans are redirected at a temporary directory or monkeypatched away,
* the driver read is replaced with a fake that returns real MJPEG bytes,
* the vision model is an injected fake, and ``get_llm_backend`` is patched to a
  tripwire so a test that accidentally reaches for the network fails loudly.

The through-line is the anti-fabrication contract: a not-seen result must say it
did not see, and a "seen" result without the model's own words must be
impossible to construct.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from claire.senses import camera

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _no_devices(monkeypatch):
    """Simulate this machine: nothing matching /dev/video* exists."""
    monkeypatch.setattr(
        camera, "_video_device_paths", lambda dev_root=camera.DEV_ROOT: []
    )


def _dev_root_with(tmp_path: Path, entries, labels=()):
    """Build a fake /dev plus sysfs metadata tree, no device is ever opened."""
    dev_root = tmp_path / "dev"
    dev_root.mkdir(exist_ok=True)
    for entry in entries:
        (dev_root / entry).write_bytes(b"")
    sysfs = tmp_path / "sys" / "class" / "video4linux"
    for video, label in labels:
        directory = sysfs / video
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "name").write_text(label, encoding="utf-8")
    return dev_root, sysfs


def _write_png(path: Path, size=(32, 24), color=(200, 40, 40)) -> Path:
    Image.new("RGB", size, color).save(path, "PNG")
    return path


def _jpeg_bytes(size=(64, 48), color=(20, 160, 90)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def _config(**overrides):
    values = dict(
        llm_provider="omniroute",
        llm_base_url=camera.OMNIROUTE_BASE_URL,
        llm_api_key="test-key",
        llm_chat_model="cmd/deepseek/deepseek-v4.1-flash",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingDescriber:
    """A describer that records its arguments and returns a fixed answer."""

    def __init__(self, answer="A red mug on a wooden desk."):
        self.answer = answer
        self.calls = []

    def describe(self, image_path, question):
        self.calls.append((Path(image_path), question))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class FakeBackend:
    """Stands in for claire.llm's backend; records chat calls, no HTTP."""

    def __init__(self, response=None):
        self.response = response if response is not None else {
            "message": {"content": "A red mug on a wooden desk."}
        }
        self.calls = []

    def chat(self, chat_model, messages, timeout_sec=30.0, **kwargs):
        self.calls.append(
            {"model": chat_model, "messages": messages, "timeout_sec": timeout_sec}
        )
        return self.response


@pytest.fixture(autouse=True)
def _no_router_key(monkeypatch):
    """This machine's real key must never leak into a test's expectations."""
    monkeypatch.delenv(camera.OMNIROUTE_KEY_ENV, raising=False)


# --------------------------------------------------------------------------- #
# detect_cameras
# --------------------------------------------------------------------------- #


def test_a_machine_with_no_video_devices_yields_an_empty_list(tmp_path):
    """No video devices is a valid answer, and must not crash or invent one.

    This test used to assert the *machine's* state - "this one has no camera" -
    by calling ``detect_cameras()`` with no arguments and asserting ``[]``. That
    became false the moment a USB webcam was plugged in, and it would then fail
    for a reason that was entirely good news. Machine state belongs in the
    acceptance scoreboard, which reports it with context; a unit test should pin
    behaviour, so this points the scan at an empty directory instead.
    """
    assert camera.detect_cameras(tmp_path) == []
    assert list(camera.detect_cameras(tmp_path)) == []


def test_detect_cameras_reads_devices_and_sysfs_metadata(tmp_path):
    dev_root, sysfs = _dev_root_with(
        tmp_path,
        ["video0", "video2"],
        labels=[("video0", "Integrated Camera"), ("video2", "USB Webcam")],
    )
    devices = camera.detect_cameras(dev_root, sysfs_root=sysfs)
    assert [device.index for device in devices] == [0, 2]
    assert [device.name for device in devices] == ["Integrated Camera", "USB Webcam"]
    assert [device.path for device in devices] == [
        str(dev_root / "video0"),
        str(dev_root / "video2"),
    ]
    assert devices[0].label == f"Integrated Camera ({dev_root / 'video0'})"


def test_detect_cameras_ignores_entries_that_are_not_video_devices(tmp_path):
    dev_root, sysfs = _dev_root_with(
        tmp_path, ["video0", "video-helper", "video", "camera0", "video1.bak"]
    )
    devices = camera.detect_cameras(dev_root, sysfs_root=sysfs)
    assert [device.path for device in devices] == [str(dev_root / "video0")]


def test_detect_cameras_works_without_metadata_and_never_opens_the_device(tmp_path):
    dev_root, sysfs = _dev_root_with(tmp_path, ["video3"])
    devices = camera.detect_cameras(dev_root, sysfs_root=sysfs, read_metadata=False)
    assert len(devices) == 1
    assert devices[0].name == ""
    assert devices[0].label == str(dev_root / "video3")


# --------------------------------------------------------------------------- #
# describe_availability / diagnose
# --------------------------------------------------------------------------- #


def test_describe_availability_without_a_camera_is_a_plain_report_not_an_error(monkeypatch):
    _no_devices(monkeypatch)
    verdict = camera.describe_availability()
    assert verdict.available is False
    assert verdict.devices == ()
    assert "/dev/video*" in verdict.reason
    assert list(verdict.scanned) == ["/dev/video*"]
    advice = " ".join(verdict.actions).lower()
    assert "plug in" in advice
    assert "phone" in advice
    assert "do not have a camera" in verdict.spoken_summary()
    assert "not available" in verdict.render()
    assert "What you can do" in verdict.render()


def test_describe_availability_with_a_camera_reports_it(tmp_path):
    dev_root, sysfs = _dev_root_with(
        tmp_path, ["video0"], labels=[("video0", "Integrated Camera")]
    )
    verdict = camera.describe_availability(dev_root, sysfs_root=sysfs)
    assert verdict.available is True
    assert len(verdict.devices) == 1
    assert "Integrated Camera" in verdict.render()
    assert "1 camera" in verdict.reason
    assert verdict.spoken_summary().startswith("Yes")
    assert verdict.actions == ()


def test_diagnose_matches_describe_availability(monkeypatch):
    _no_devices(monkeypatch)
    assert camera.diagnose() == camera.describe_availability()


# --------------------------------------------------------------------------- #
# capture_frame without a camera
# --------------------------------------------------------------------------- #


def test_capture_frame_without_a_camera_raises_a_named_error_with_the_fix(monkeypatch):
    _no_devices(monkeypatch)
    with pytest.raises(camera.CameraNotFoundError) as info:
        camera.capture_frame()
    error = info.value
    assert isinstance(error, camera.CameraError)
    assert isinstance(error, RuntimeError)
    assert error.scanned == ["/dev/video*"]
    assert "/dev/video*" in str(error)
    assert "does not exist" not in str(error)
    assert error.remedy
    lowered = str(error).lower()
    assert "plug in a usb webcam" in lowered
    assert "modprobe uvcvideo" in lowered
    assert "phone" in lowered
    assert "not a crash" in lowered


def test_capture_frame_never_opens_a_device_when_the_scan_is_empty(monkeypatch):
    _no_devices(monkeypatch)

    def _tripwire(*args, **kwargs):
        raise AssertionError("capture_frame opened a device with no camera present")

    monkeypatch.setattr(camera, "_capture_v4l2_read", _tripwire)
    with pytest.raises(camera.CameraNotFoundError):
        camera.capture_frame()


def test_capture_frame_rejects_an_unnamed_device_and_lists_the_attached_ones(tmp_path):
    dev_root, sysfs = _dev_root_with(tmp_path, ["video0"])
    with pytest.raises(camera.CameraNotFoundError) as info:
        camera.capture_frame("video9", dev_root=dev_root, sysfs_root=sysfs)
    error = info.value
    assert error.requested == "video9"
    assert error.available == [str(dev_root / "video0")]
    assert "video9" in str(error)
    assert "/dev/video*" in str(error)


# --------------------------------------------------------------------------- #
# capture_frame with a fake driver (still no real device)
# --------------------------------------------------------------------------- #


def test_capture_frame_saves_a_real_jpeg_from_a_fake_driver_under_the_lock(tmp_path, monkeypatch):
    dev_root, sysfs = _dev_root_with(
        tmp_path, ["video0"], labels=[("video0", "Fake Camera")]
    )
    jpeg = _jpeg_bytes(size=(64, 48))
    observed = {}

    def fake_capture(device_path, **kwargs):
        observed["device_path"] = str(device_path)
        observed["lock_held"] = camera.camera_lock._is_owned()
        return camera.CapturedFrame(
            data=jpeg, pixel_format="MJPG", width=64, height=48
        )

    monkeypatch.setattr(camera, "_capture_v4l2_read", fake_capture)
    out_dir = tmp_path / "frames"
    saved = camera.capture_frame(
        dev_root=dev_root, sysfs_root=sysfs, out_dir=out_dir
    )
    assert isinstance(saved, Path)
    assert saved.is_file()
    assert saved.parent == out_dir
    assert observed["device_path"] == str(dev_root / "video0")
    assert observed["lock_held"] is True, "camera capture must hold camera_lock"
    with Image.open(saved) as image:
        assert image.format == "JPEG"
        assert image.size == (64, 48)


def test_capture_frame_selects_by_index_and_by_name(tmp_path, monkeypatch):
    dev_root, sysfs = _dev_root_with(
        tmp_path, ["video0", "video1"], labels=[("video1", "USB Webcam")]
    )
    seen = []

    def fake_capture(device_path, **kwargs):
        seen.append(str(device_path))
        return camera.CapturedFrame(
            data=_jpeg_bytes(), pixel_format="MJPG", width=64, height=48
        )

    monkeypatch.setattr(camera, "_capture_v4l2_read", fake_capture)
    camera.capture_frame(1, dev_root=dev_root, sysfs_root=sysfs, out_dir=tmp_path)
    camera.capture_frame("usb webcam", dev_root=dev_root, sysfs_root=sysfs, out_dir=tmp_path)
    assert seen == [str(dev_root / "video1"), str(dev_root / "video1")]


def test_captured_frame_refuses_to_be_empty():
    with pytest.raises(ValueError):
        camera.CapturedFrame(data=b"", pixel_format="MJPG", width=1, height=1)


def test_unopenable_device_is_a_named_error_with_the_permission_fix():
    with pytest.raises(camera.CameraCaptureError) as info:
        camera._capture_v4l2_read("/dev/video-does-not-exist")
    error = info.value
    assert error.device_path == "/dev/video-does-not-exist"
    assert "could not be opened" in str(error)
    assert "usermod -aG video" in str(error)


def test_extract_frame_pulls_one_complete_mjpeg_frame():
    jpeg = _jpeg_bytes()
    assert camera._extract_frame(b"junk" + jpeg + b"tail", "MJPG", 64, 48) == jpeg
    assert camera._extract_frame(jpeg[:10], "MJPG", 64, 48) is None


def test_extract_frame_waits_for_a_whole_yuyv_frame():
    assert camera._extract_frame(b"\x00" * 7, "YUYV", 4, 1) is None
    assert camera._extract_frame(b"\x01\x02\x03\x04\x05", "YUYV", 2, 1) == b"\x01\x02\x03\x04"


def test_yuyv_expansion_shares_chroma_between_pixel_pairs():
    expanded = camera._yuyv_to_ycbcr(bytes([10, 20, 30, 40]), 2, 1)
    assert expanded == bytes([10, 20, 40, 30, 20, 40])


def test_missing_pillow_error_names_the_install_command():
    error = camera.MissingDependencyError("PIL")
    assert error.package == "pillow"
    assert "-m pip install pillow" in str(error)
    assert error.remedy == error.command


# --------------------------------------------------------------------------- #
# describe_image: precondition raises, so nothing is described that was not seen
# --------------------------------------------------------------------------- #


def test_describe_image_without_a_real_frame_path_never_touches_the_describer(tmp_path):
    spy = RecordingDescriber()
    with pytest.raises(camera.FrameNotFoundError) as info:
        camera.describe_image(tmp_path / "never_captured.jpg", "what is this?", describer=spy)
    assert spy.calls == []
    assert "never_captured.jpg" in str(info.value)
    assert "does not exist" in str(info.value)
    assert info.value.remedy


def test_describe_image_rejects_a_path_that_is_not_an_image(tmp_path):
    not_an_image = tmp_path / "notes.jpg"
    not_an_image.write_text("this is not a picture", encoding="utf-8")
    spy = RecordingDescriber()
    with pytest.raises(camera.NotAnImageError) as info:
        camera.describe_image(not_an_image, describer=spy)
    assert spy.calls == []
    assert "Not an image" in str(info.value)


def test_describe_image_rejects_a_directory_path(tmp_path):
    spy = RecordingDescriber()
    with pytest.raises(camera.FrameNotFoundError):
        camera.describe_image(tmp_path, describer=spy)
    assert spy.calls == []


# --------------------------------------------------------------------------- #
# describe_image with an injected describer
# --------------------------------------------------------------------------- #


def test_injected_describer_returns_the_description(tmp_path):
    frame = _write_png(tmp_path / "frame.png")
    spy = RecordingDescriber("A red mug on a wooden desk.")
    result = camera.describe_image(frame, "what is on the desk?", describer=spy)
    assert result.seen is True
    assert result.status == camera.STATUS_SEEN
    assert result.description == "A red mug on a wooden desk."
    assert result.error is None
    assert result.image_path == str(frame)
    assert result.question == "what is on the desk?"
    assert spy.calls == [(frame, "what is on the desk?")]
    assert "A red mug" in result.spoken_summary()
    assert "Seen" in result.render()


def test_a_plain_callable_is_accepted_as_a_describer(tmp_path):
    frame = _write_png(tmp_path / "frame.png")
    result = camera.describe_image(
        frame, "anyone there?", describer=lambda path, question: f"saw {Path(path).name} for {question}"
    )
    assert result.seen is True
    assert result.description == "saw frame.png for anyone there?"


def test_a_silent_describer_is_reported_as_not_seen_not_as_an_empty_picture(tmp_path):
    frame = _write_png(tmp_path / "frame.png")
    result = camera.describe_image(frame, "what is this?", describer=RecordingDescriber("   "))
    assert result.seen is False
    assert result.status == camera.STATUS_NOT_SEEN
    assert result.description == ""
    assert isinstance(result.error, camera.VisionNoAnswerError)
    assert "empty" in str(result.error).lower()
    assert result.remedy
    spoken = result.spoken_summary()
    assert "could not see" in spoken
    assert "Not seen" in result.render()


def test_a_failing_describer_returns_a_named_not_seen_result(tmp_path):
    frame = _write_png(tmp_path / "frame.png")
    result = camera.describe_image(
        frame, describer=RecordingDescriber(RuntimeError("the router exploded"))
    )
    assert result.seen is False
    assert result.description == ""
    assert isinstance(result.error, camera.VisionRequestError)
    assert "the router exploded" in str(result.error)
    assert "curl" in result.remedy


def test_a_describer_that_is_not_callable_is_a_named_error_not_a_crash(tmp_path):
    frame = _write_png(tmp_path / "frame.png")
    result = camera.describe_image(frame, describer=object())
    assert result.seen is False
    assert isinstance(result.error, camera.VisionUnavailableError)
    assert "not callable" in str(result.error)


def test_describe_image_or_raise_surfaces_the_named_error(tmp_path):
    frame = _write_png(tmp_path / "frame.png")
    with pytest.raises(camera.VisionUnavailableError):
        camera.describe_image_or_raise(
            frame,
            describer=RecordingDescriber(
                camera.VisionUnavailableError("no route", remedy="export X=1")
            ),
        )
    ok = camera.describe_image_or_raise(
        frame, describer=RecordingDescriber("A bright window.")
    )
    assert ok.seen is True
    assert ok.description == "A bright window."


def test_image_description_refuses_a_seen_result_with_no_words(tmp_path):
    with pytest.raises(ValueError, match="Refusing to report 'seen'"):
        camera.ImageDescription(
            seen=True, description="   ", image_path=str(tmp_path / "f.png")
        )
    with pytest.raises(ValueError, match="named error"):
        camera.ImageDescription(seen=False, description="", image_path="x.png")


# --------------------------------------------------------------------------- #
# The default describer: project LLM reuse, key handling, no network
# --------------------------------------------------------------------------- #


def test_cloud_describer_reuses_the_project_backend_and_builds_a_data_url(tmp_path):
    frame = _write_png(tmp_path / "frame.png", size=(8, 6))
    backend = FakeBackend()
    describer = camera.CloudVisionDescriber(_config(), backend=backend)
    text = describer.describe(frame, "what is this?")
    assert text == "A red mug on a wooden desk."
    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["model"] == "cmd/deepseek/deepseek-v4.1-flash"
    assert call["messages"][0]["role"] == "system"
    assert "never invent" in call["messages"][0]["content"].lower()
    user_parts = call["messages"][1]["content"]
    assert user_parts[0]["text"] == "what is this?"
    data_url = user_parts[1]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == frame.read_bytes()


def test_cloud_describer_uses_the_default_question_when_none_is_given(tmp_path):
    frame = _write_png(tmp_path / "frame.png")
    backend = FakeBackend()
    camera.CloudVisionDescriber(_config(), backend=backend).describe(frame)
    parts = backend.calls[0]["messages"][1]["content"]
    assert parts[0]["text"] == camera.DEFAULT_VISION_QUESTION


def test_cloud_describer_refuses_an_empty_model_answer(tmp_path):
    frame = _write_png(tmp_path / "frame.png")
    backend = FakeBackend(response={"message": {"content": "   "}})
    describer = camera.CloudVisionDescriber(_config(), backend=backend)
    with pytest.raises(camera.VisionNoAnswerError):
        describer.describe(frame, "anything?")


def test_cloud_describer_requires_a_model():
    with pytest.raises(camera.VisionUnavailableError) as info:
        camera.CloudVisionDescriber(_config(llm_chat_model=""), backend=FakeBackend())
    assert "llm_chat_model" in info.value.remedy


def test_cloud_describer_requires_the_omniroute_key():
    with pytest.raises(camera.VisionUnavailableError) as info:
        camera.CloudVisionDescriber(
            _config(llm_api_key="", llm_provider="omniroute"), backend=FakeBackend()
        )
    assert camera.OMNIROUTE_KEY_ENV in info.value.remedy
    assert "export" in info.value.remedy


def test_cloud_describer_accepts_a_local_route_without_a_key():
    config = _config(
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:8080/v1",
        llm_api_key="",
    )
    describer = camera.CloudVisionDescriber(config, backend=FakeBackend())
    assert describer.model == config.llm_chat_model


def test_absent_key_returns_a_named_not_seen_result_and_never_calls_the_network(
    tmp_path, monkeypatch
):
    frame = _write_png(tmp_path / "frame.png")
    reached_network = []

    def tripwire(config):
        reached_network.append(config)
        raise AssertionError("describe_image built a backend instead of failing on the key")

    monkeypatch.setattr(camera, "load_settings", lambda: _config(llm_api_key=""))
    monkeypatch.setattr(camera, "get_llm_backend", tripwire)

    result = camera.describe_image(frame, "what is this?")
    assert reached_network == []
    assert result.seen is False
    assert result.description == ""
    assert isinstance(result.error, camera.VisionUnavailableError)
    assert camera.OMNIROUTE_KEY_ENV in result.remedy
    assert "could not see" in result.spoken_summary()


def test_describe_image_without_a_describer_uses_the_configured_settings(tmp_path, monkeypatch):
    frame = _write_png(tmp_path / "frame.png")
    backend = FakeBackend(response={"message": {"content": "A small red square."}})
    monkeypatch.setattr(camera, "load_settings", lambda: _config())
    monkeypatch.setattr(camera, "get_llm_backend", lambda config: backend)

    result = camera.describe_image(frame, "what colour?")
    assert result.seen is True
    assert result.description == "A small red square."
    assert backend.calls[0]["model"] == "cmd/deepseek/deepseek-v4.1-flash"


def test_a_config_load_failure_is_a_named_not_seen_result(tmp_path, monkeypatch):
    frame = _write_png(tmp_path / "frame.png")

    def explode():
        raise RuntimeError("config.json is unreadable")

    monkeypatch.setattr(camera, "load_settings", explode)
    result = camera.describe_image(frame, "what is this?")
    assert result.seen is False
    assert isinstance(result.error, camera.VisionUnavailableError)
    assert "config.json is unreadable" in str(result.error)


def test_timeout_failure_is_classified_with_a_remedy(tmp_path, monkeypatch):
    frame = _write_png(tmp_path / "frame.png")
    import requests

    def explode(path, question):
        raise requests.exceptions.ConnectTimeout("too slow")

    result = camera.describe_image(
        frame, "what is this?", describer=explode, config=_config()
    )
    assert result.seen is False
    assert isinstance(result.error, camera.VisionRequestError)
    assert "did not answer" in str(result.error)
    assert "models" in result.remedy


def test_unreachable_router_is_classified_as_a_named_request_error(tmp_path):
    frame = _write_png(tmp_path / "frame.png")

    def explode(path, question):
        raise ConnectionError("Connection refused")

    result = camera.describe_image(
        frame, "what is this?", describer=explode, config=_config()
    )
    assert result.seen is False
    assert isinstance(result.error, camera.VisionRequestError)
    assert "could not be reached" in str(result.error)
    assert "OmniRouter" in result.remedy


def test_unauthorized_response_is_classified_as_a_key_problem(tmp_path):
    frame = _write_png(tmp_path / "frame.png")
    import requests

    response = requests.Response()
    response.status_code = 401

    def explode(path, question):
        error = requests.exceptions.HTTPError("401 Client Error")
        error.response = response
        raise error

    result = camera.describe_image(
        frame, "what is this?", describer=explode, config=_config()
    )
    assert result.seen is False
    assert isinstance(result.error, camera.VisionUnavailableError)
    assert camera.OMNIROUTE_KEY_ENV in result.remedy
