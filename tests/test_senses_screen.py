"""Tests for reading text off the screen (R-OCR-1).

The machine this runs on has **no** ``tesseract`` binary: ``pytesseract`` is
installed but has no engine behind it. That is the case these tests pin, because
the failure mode it must never have is upstream's - returning ``''`` as if the
screen were blank.

Nothing here captures a real screen, runs real OCR or touches the network: the
capture mechanism and the OCR engine are both injected seams, and every image is
a tiny PNG written into ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claire.senses import screen as sc

# The gate runs `pytest -m unit`; without this marker these tests are
# DESELECTED, so they would pass while never running at all. Do not remove.
pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fakes and helpers. No real screen, no real binary.
# --------------------------------------------------------------------------- #


def write_png(path: Path, size: tuple[int, int] = (400, 300)) -> Path:
    """Write a real (but blank) PNG so the byte-size and geometry code is exercised."""

    from PIL import Image

    Image.new("RGB", size, (250, 250, 250)).save(path, "PNG")
    return path


class FakeCapture:
    """A capture_fn that writes a tiny PNG and records the mechanism name."""

    def __init__(self, size: tuple[int, int] = (400, 300), mechanism: str = "fake-capture") -> None:
        self.size = size
        self.mechanism = mechanism
        self.calls: list[Path] = []

    def __call__(self, destination: Path) -> str:
        self.calls.append(Path(destination))
        write_png(Path(destination), self.size)
        return self.mechanism


class FakeOcrEngine:
    """A stand-in OCR engine: returns fixed text and optional word boxes."""

    name = "fake-ocr"

    def __init__(self, text: str = "", words: list[sc.OcrWord] | None = None) -> None:
        self.text = text
        self.words = words or []
        self.read_paths: list[Path] = []

    def image_to_text(self, image_path: str | Path) -> str:
        self.read_paths.append(Path(image_path))
        return self.text

    def image_to_words(self, image_path: str | Path) -> list[sc.OcrWord]:
        return list(self.words)


class MinimalFakeEngine:
    """The protocol's minimum: a name and image_to_text, no word boxes."""

    name = "minimal-fake-ocr"

    def __init__(self, text: str = "") -> None:
        self.text = text

    def image_to_text(self, image_path: str | Path) -> str:
        return self.text


# --------------------------------------------------------------------------- #
# The real case on this machine: the binary is absent.
# --------------------------------------------------------------------------- #


def test_missing_binary_raises_named_error_naming_the_fix(tmp_path):
    """Absent `tesseract` must raise OcrEngineMissingError, naming binary and package."""

    image = write_png(tmp_path / "shot.png")
    engine = sc.TesseractOcrEngine(which=lambda name: None)

    with pytest.raises(sc.OcrEngineMissingError) as excinfo:
        sc.ocr_image(image, engine=engine)

    error = excinfo.value
    message = str(error)

    # It is *our* named error, not pytesseract's TesseractNotFoundError leaking out.
    assert type(error) is sc.OcrEngineMissingError
    assert isinstance(error, sc.ScreenError)
    assert error.__cause__ is None
    # The binary is named...
    assert error.binary == "tesseract"
    assert "tesseract" in message
    assert "not installed" in message
    # ...and so is the package to install, with a copy-pasteable command.
    assert error.package, "the error must name a package to install"
    assert error.package in message
    assert error.command.startswith("sudo ")
    assert error.command in message
    assert "install" in message.lower()
    assert "\n    " in message, "the fix is a copy-pasteable command on its own line"


def test_missing_binary_never_returns_an_empty_string(tmp_path):
    """The empty string is reserved for a blank screen, never for a broken engine."""

    image = write_png(tmp_path / "shot.png")
    engine = sc.TesseractOcrEngine(which=lambda name: None)

    with pytest.raises(sc.OcrEngineMissingError):
        sc.ocr_image(image, engine=engine)


def test_the_fix_names_a_package_this_distro_actually_uses(tmp_path):
    """The package name comes from /etc/os-release, so the command is real here."""

    error = sc.TesseractOcrEngine(which=lambda name: None).missing_binary_error()
    hint = sc.distro_hint()

    assert error.distro == hint.distro_id
    assert error.command == hint.command(error.package)
    assert error.package, "a package name must be part of the fix"
    if hint.family == "debian":
        assert error.package == "tesseract-ocr"
        assert "apt-get" in error.command


def test_the_real_binary_resolver_agrees_with_the_machine(tmp_path):
    """On this machine tesseract is genuinely absent: the honest default path."""

    import shutil as real_shutil

    engine = sc.TesseractOcrEngine()
    found = real_shutil.which("tesseract")

    if found is None:
        # The measured fact: pytesseract importable, binary missing -> named error.
        assert engine.binary_path() is None
        assert engine.module_available() is True  # pytesseract *is* installed...
        assert engine.available() is False  # ...but it has no engine behind it
        with pytest.raises(sc.OcrEngineMissingError) as excinfo:
            sc.ocr_image(write_png(tmp_path / "shot.png"), engine=engine)
        assert "tesseract-ocr" in str(excinfo.value) or "install" in str(excinfo.value)
    else:  # pragma: no cover - only on a machine that has the binary
        assert engine.binary_path() == found
        assert engine.available() is True


# --------------------------------------------------------------------------- #
# diagnose() / available(): a structured verdict, not a guess.
# --------------------------------------------------------------------------- #


def test_diagnose_reports_a_structured_verdict():
    absent = sc.diagnose(sc.TesseractOcrEngine(which=lambda name: None))

    assert isinstance(absent, sc.OcrDiagnosis)
    payload = absent.to_dict()
    for key in (
        "engine",
        "binary",
        "binary_path",
        "binary_present",
        "python_module",
        "python_module_present",
        "available",
        "distro",
        "distro_family",
        "package",
        "fix_command",
        "reason",
    ):
        assert key in payload

    assert absent.binary == "tesseract"
    assert absent.binary_path is None
    assert absent.binary_present is False
    assert absent.available is False
    assert absent.distro and absent.distro_family
    assert "tesseract" in absent.reason
    assert "install" in absent.reason.lower()
    assert absent.fix_command in absent.reason or "install" in absent.reason


def test_diagnose_and_available_agree_on_both_states(monkeypatch):
    def fake_which_found(name: str) -> str | None:
        return "/usr/bin/tesseract"

    monkeypatch.setattr(sc.shutil, "which", fake_which_found)
    present = sc.diagnose()
    assert present.binary_present is True
    assert present.binary_path == "/usr/bin/tesseract"
    assert sc.available() == present.available == present.python_module_present

    monkeypatch.setattr(sc.shutil, "which", lambda name: None)
    missing = sc.diagnose()
    assert missing.binary_present is False
    assert missing.available is False
    assert sc.available() is False
    assert isinstance(missing.as_error(), sc.OcrEngineMissingError)
    assert missing.package in str(missing.as_error())


def test_diagnose_takes_an_injected_engine_at_face_value():
    """A test double with no binary notion must not be judged on tesseract's."""

    verdict = sc.diagnose(FakeOcrEngine("hi"))
    assert verdict.available is True
    assert verdict.engine == "fake-ocr"


# --------------------------------------------------------------------------- #
# The injected OCR seam.
# --------------------------------------------------------------------------- #


def test_injected_fake_engine_returns_text(tmp_path):
    engine = FakeOcrEngine("Quarterly Report\nTotal: 42\n")
    capture = FakeCapture()

    result = sc.read_screen(destination=tmp_path / "out.png", engine=engine, capture_fn=capture)

    assert result.text == "Quarterly Report\nTotal: 42\n"
    assert result.has_text is True
    assert result.status == sc.STATUS_TEXT == "ocr_text"
    assert result.ocr_ran is True
    assert result.ocr_mechanism == "fake-ocr"
    assert result.capture_mechanism == "fake-capture"
    assert result.capture_path.exists()
    assert result.byte_size == result.capture_path.stat().st_size > 0
    assert engine.read_paths == [result.capture_path]
    assert capture.calls == [result.capture_path]
    # The verdict is carried along, so a caller can inspect it without re-probing.
    assert result.diagnosis.available is True
    assert result.to_dict()["status"] == "ocr_text"


def test_satisfies_the_ocr_engine_protocol():
    assert isinstance(FakeOcrEngine("x"), sc.OcrEngine)
    assert isinstance(sc.TesseractOcrEngine(), sc.OcrEngine)


def test_ocr_image_routes_through_the_injected_engine(tmp_path):
    image = write_png(tmp_path / "already.png")
    engine = FakeOcrEngine("hello from the seam")

    assert sc.ocr_image(image, engine=engine) == "hello from the seam"
    assert engine.read_paths == [image]


def test_ocr_image_on_a_directory_is_a_named_error(tmp_path):
    with pytest.raises(sc.ScreenImageNotFoundError):
        sc.ocr_image(tmp_path, engine=FakeOcrEngine("x"))


def test_read_screen_refuses_when_the_engine_says_it_cannot_run(tmp_path):
    class BrokenEngine(FakeOcrEngine):
        binary = "fake-tesseract"

        def available(self) -> bool:
            return False

    engine = BrokenEngine("should never be reached")

    with pytest.raises(sc.OcrEngineMissingError) as excinfo:
        sc.read_screen(destination=tmp_path / "out.png", engine=engine, capture_fn=FakeCapture())

    assert "fake-tesseract" in str(excinfo.value)
    assert engine.read_paths == [], "the engine must not be called when it cannot run"


# --------------------------------------------------------------------------- #
# A nonexistent image names the path it looked for.
# --------------------------------------------------------------------------- #


def test_nonexistent_image_raises_named_error_naming_the_path(tmp_path):
    missing = tmp_path / "not-there.png"

    with pytest.raises(sc.ScreenImageNotFoundError) as excinfo:
        sc.ocr_image(missing)

    error = excinfo.value
    assert isinstance(error, sc.ScreenError)
    assert error.path == str(missing)
    assert str(missing) in str(error)
    assert "not found" in str(error).lower()
    assert type(error) is sc.ScreenImageNotFoundError


def test_nonexistent_image_still_names_the_path_with_an_engine(tmp_path):
    missing = tmp_path / "ghost.png"
    engine = FakeOcrEngine("text")

    with pytest.raises(sc.ScreenImageNotFoundError) as excinfo:
        sc.ocr_image(missing, engine=engine)

    assert str(missing) in str(excinfo.value)
    assert engine.read_paths == []


# --------------------------------------------------------------------------- #
# Empty text is not "OCR never ran".
# --------------------------------------------------------------------------- #


def test_empty_text_is_distinguishable_from_not_run(tmp_path):
    engine = FakeOcrEngine("")  # OCR ran; the screen simply had no words.

    blank = sc.read_screen(destination=tmp_path / "blank.png", engine=engine, capture_fn=FakeCapture())
    never = sc.read_screen(
        destination=tmp_path / "never.png", engine=engine, capture_fn=FakeCapture(), ocr=False
    )

    # Both have empty text and no usable words...
    assert blank.text == "" and blank.has_text is False
    assert never.text == "" and never.has_text is False
    # ...but only one of them ran OCR, and the result says which.
    assert blank.status == sc.STATUS_EMPTY == "ocr_empty"
    assert blank.ocr_ran is True
    assert blank.is_blank_screen is True
    assert blank.ocr_mechanism == "fake-ocr"

    assert never.status == sc.STATUS_NOT_RUN == "ocr_not_run"
    assert never.ocr_ran is False
    assert never.is_blank_screen is False, "a capture that was never read is not a blank screen"
    assert never.ocr_mechanism == "none"
    assert "not requested" in never.note.lower()

    assert blank.status != never.status
    assert blank.to_dict()["ocr_ran"] is True
    assert never.to_dict()["ocr_ran"] is False
    # The capture itself still happened in both cases and is reported.
    assert blank.capture_path.exists() and never.capture_path.exists()
    assert blank.byte_size > 0 and never.byte_size > 0


def test_whitespace_only_text_counts_as_blank_not_as_text(tmp_path):
    result = sc.read_screen(
        destination=tmp_path / "ws.png", engine=FakeOcrEngine("   \n\t\n"), capture_fn=FakeCapture()
    )

    assert result.ocr_ran is True
    assert result.status == sc.STATUS_EMPTY
    assert result.has_text is False
    assert result.is_blank_screen is True


# --------------------------------------------------------------------------- #
# capture_screen(): the mechanism that actually worked is reported.
# --------------------------------------------------------------------------- #


def test_capture_reports_the_mechanism_and_the_byte_size(tmp_path):
    capture = FakeCapture(size=(640, 480), mechanism="pillow-imagegrab")

    result = sc.capture_screen(destination=tmp_path / "cap.png", capture_fn=capture)

    assert isinstance(result, sc.CaptureResult)
    assert result.mechanism == "pillow-imagegrab"
    assert result.path == tmp_path / "cap.png"
    assert result.byte_size > 0


def test_capture_creates_missing_parent_directories(tmp_path):
    capture = FakeCapture()
    destination = tmp_path / "nested" / "deeper" / "cap.png"

    result = sc.capture_screen(destination=destination, capture_fn=capture)

    assert destination.exists()
    assert result.byte_size > 0


def test_capture_unavailable_lists_every_mechanism_and_the_command_to_fix_it(
    tmp_path, monkeypatch
):
    def failing(name):
        def attempt(destination: Path) -> str:
            raise sc.ScreenCaptureUnavailableError([(name, "simulated absence")])

        return attempt

    monkeypatch.setattr(
        sc,
        "_capture_mechanisms",
        lambda: [("pillow-imagegrab", failing("pillow-imagegrab")), ("scrot", failing("scrot"))],
    )

    with pytest.raises(sc.ScreenCaptureUnavailableError) as excinfo:
        sc.capture_screen(destination=tmp_path / "cap.png")

    message = str(excinfo.value)
    assert "pillow-imagegrab" in message
    assert "scrot" in message
    assert excinfo.value.attempts == [
        ("pillow-imagegrab", "simulated absence"),
        ("scrot", "simulated absence"),
    ]
    # And the fix is a command, not advice.
    assert "install" in message.lower()
    assert any("sudo" in fix for fix in excinfo.value.fixes)


def test_capture_rejects_a_zero_byte_png(tmp_path):
    with pytest.raises(sc.ScreenCaptureUnavailableError):
        sc.CaptureResult(path=tmp_path / "empty.png", byte_size=0, mechanism="fake")


def test_capture_mechanism_list_prefers_the_python_native_route(monkeypatch):
    monkeypatch.setattr(sc, "sys_platform", lambda: "linux")
    names = [name for name, _ in sc._capture_mechanisms()]

    assert names[0] == "pillow-imagegrab", "the Python-native grab needs no extra package"
    assert "scrot" in names and "imagemagick-import" in names and "grim" in names
    assert "screencapture (upstream screenshot mechanism)" not in names


def test_upstream_screenshot_module_is_reused_not_duplicated():
    """Upstream's tool is imported (never edited) for its macOS mechanism name."""

    import claire.tools.builtin.screenshot as upstream

    assert upstream.ScreenshotTool().name == "screenshot"
    # Upstream's own route on this Linux machine is exactly the silent empty string
    # this module exists to avoid - documented, and compensated for here.
    assert sc.TesseractOcrEngine().binary == "tesseract"


# --------------------------------------------------------------------------- #
# Finding a phrase: the actual use case.
# --------------------------------------------------------------------------- #


SCREEN_TEXT = (
    "File  Edit  View\n"
    "Recent Documents\n"
    "Quarterly Report\n"
    "Total: 42\n"
    "\n"
    "Cancel   OK\n"
    "status bar hidden\n"
    "footer note\n"
)


def test_find_phrase_reports_the_line_and_a_region():
    location = sc.find_phrase(SCREEN_TEXT, "Quarterly Report")

    assert isinstance(location, sc.PhraseLocation)
    assert location.found is True
    assert location.line_number == 3
    assert location.line_text == "Quarterly Report"
    assert location.column == 0
    assert location.region in sc._VERTICAL_BANDS
    assert location.match_count == 1
    assert location.quarter is None, "text alone cannot say which half of the width"
    assert location.source == "ocr_text_only"
    assert "line 3" in location.describe()
    assert "Quarterly Report" in location.describe()


def test_find_phrase_is_case_insensitive_by_default_and_off_on_request():
    assert sc.find_phrase(SCREEN_TEXT, "quarterly report").found is True
    assert sc.find_phrase(SCREEN_TEXT, "quarterly report", case_sensitive=True).found is False
    assert sc.find_phrase(SCREEN_TEXT, "Quarterly Report", case_sensitive=True).found is True


def test_find_phrase_reports_the_vertical_quarter():
    top = sc.find_phrase(SCREEN_TEXT, "File  Edit")
    bottom = sc.find_phrase(SCREEN_TEXT, "footer note")

    assert top.region == "top"
    assert bottom.region == "bottom"
    assert top.line_number == 1
    assert bottom.line_number == 8


def test_find_phrase_not_found_is_a_result_not_an_exception():
    location = sc.find_phrase(SCREEN_TEXT, "definitely absent")

    assert location.found is False
    assert location.line_number is None
    assert location.region is None
    assert location.match_count == 0
    assert "not found" in location.describe()


def test_find_phrase_rejects_an_empty_needle():
    with pytest.raises(ValueError):
        sc.find_phrase(SCREEN_TEXT, "   ")


def test_find_phrase_uses_word_boxes_for_a_true_quadrant():
    words = [
        sc.OcrWord(text="Cancel", left=10, top=10, width=60, height=16, line_number=1),
        sc.OcrWord(text="OK", left=700, top=560, width=40, height=16, line_number=1),
    ]

    top_left = sc.find_phrase(SCREEN_TEXT, "Cancel", image_size=(800, 600), words=words)
    bottom_right = sc.find_phrase(SCREEN_TEXT, "OK", image_size=(800, 600), words=words)

    assert top_left.source == "ocr_words"
    assert top_left.quarter == "top-left"
    assert top_left.region == "top"
    assert bottom_right.quarter == "bottom-right"
    assert bottom_right.region == "bottom"
    assert bottom_right.image_size == (800, 600)


def test_find_phrase_on_screen_captures_then_locates(tmp_path):
    engine = FakeOcrEngine(
        "Close\nSave as...\nQuit\n",
        words=[sc.OcrWord(text="Save", left=620, top=40, width=50, height=18, line_number=2)],
    )

    location = sc.find_phrase_on_screen(
        "Save as",
        destination=tmp_path / "find.png",
        engine=engine,
        capture_fn=FakeCapture(size=(800, 600)),
    )

    assert location.found is True
    assert location.line_number == 2
    assert location.line_text == "Save as..."
    assert location.image_size == (800, 600)
    assert engine.read_paths, "the capture must have been handed to the engine"


def test_find_phrase_on_screen_survives_an_engine_without_word_boxes(tmp_path):
    engine = MinimalFakeEngine("Close\nSave as...\nQuit\n")

    location = sc.find_phrase_on_screen(
        "Save as",
        destination=tmp_path / "find.png",
        engine=engine,
        capture_fn=FakeCapture(),
    )

    assert location.found is True
    assert location.line_number == 2
    assert location.quarter is None
    assert location.source == "ocr_text_only"


def test_find_phrase_on_screen_reports_a_phrase_that_is_absent(tmp_path):
    location = sc.find_phrase_on_screen(
        "Pay Now",
        destination=tmp_path / "find.png",
        engine=FakeOcrEngine("Close\nSave as...\nQuit\n"),
        capture_fn=FakeCapture(),
    )

    assert location.found is False
    assert location.line_number is None
