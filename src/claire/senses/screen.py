"""Reading text off the screen (R-OCR-1).

Claire may be asked "what does this say?" or "find the button": the answer has
to come from the pixels of the screen, not from the conversation. This module
captures the screen to a PNG and reads the words on it.

Two things are deliberately *not* allowed to happen here:

* **A silent empty string.** Upstream's ``tools/builtin/screenshot.py`` walks the
  macOS ``screencapture`` path only; on Linux it finds no binary, falls through
  every branch and hands back ``""``. That is indistinguishable from "the screen
  has no words on it", which is how a broken capture looks exactly like a blank
  page. Every failure in this module is a *named* error carrying the command
  that fixes it.
* **A fabricated success.** The report of *which* capture mechanism and *which*
  OCR mechanism produced the text is part of the result, because on this machine
  the honest answer is often "Pillow grabbed the X11 root window, then
  tesseract refused to run because its binary is not installed".

The OCR call sits behind :class:`OcrEngine`, so tests inject a fake and never
need the real binary, a real screen, or a display.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

__all__ = [
    "CaptureResult",
    "OcrDiagnosis",
    "OcrEngine",
    "OcrEngineMissingError",
    "OcrFailedError",
    "OcrModuleMissingError",
    "OcrWord",
    "PhraseLocation",
    "ScreenCaptureUnavailableError",
    "ScreenError",
    "ScreenImageNotFoundError",
    "ScreenReadResult",
    "TesseractOcrEngine",
    "available",
    "capture_screen",
    "diagnose",
    "find_phrase",
    "find_phrase_on_screen",
    "ocr_image",
    "read_screen",
]

#: The OCR binary every route in this module ultimately shells out to.
TESSERACT_BINARY = "tesseract"

#: Status strings for :class:`ScreenReadResult`. ``ocr_empty`` (OCR ran and
#: found no words) and ``ocr_not_run`` (nobody was asked to read anything) are
#: distinct on purpose - conflating them is the silent-empty-success bug.
STATUS_TEXT = "ocr_text"
STATUS_EMPTY = "ocr_empty"
STATUS_NOT_RUN = "ocr_not_run"


# --------------------------------------------------------------------------- #
# Named errors. Each one names what is missing and the command that fixes it.
# --------------------------------------------------------------------------- #


class ScreenError(RuntimeError):
    """Base class for every screen-reading failure."""


class OcrEngineMissingError(ScreenError):
    """The OCR engine binary is not installed.

    This is the *expected* state on this machine: ``pytesseract`` is importable
    but the ``tesseract`` binary it shells out to is absent, so every OCR call
    fails with this error rather than returning ``''``.
    """

    def __init__(
        self,
        binary: str = TESSERACT_BINARY,
        package: str = "",
        command: str = "",
        distro: str = "",
        cause: BaseException | None = None,
    ) -> None:
        self.binary = binary
        self.package = package or binary
        self.command = command or f"sudo apt-get install -y {self.package}"
        self.distro = distro
        where = f" on {distro}" if distro else ""
        detail = (
            f"OCR engine missing: the {binary!r} binary is not installed{where}, "
            "so no text can be read off the screen. pytesseract is only a wrapper "
            f"around {binary!r} and cannot read an image without it.\n"
            f"Fix: install the {self.package!r} package, i.e. run:\n"
            f"    {self.command}"
        )
        super().__init__(detail)
        if cause is not None:
            self.__cause__ = cause


class OcrModuleMissingError(ScreenError):
    """The Python OCR wrapper is not importable.

    Names the import that failed and the pip command that provides it.
    """

    def __init__(self, module: str, package: str, cause: BaseException | None = None) -> None:
        self.module = module
        self.package = package
        self.command = f"pip install {package}"
        super().__init__(
            f"OCR Python module {module!r} is not importable, so the screen cannot be "
            f"read. Fix: install the {package!r} package, i.e. run:\n"
            f"    {self.command}"
        )
        if cause is not None:
            self.__cause__ = cause


class OcrFailedError(ScreenError):
    """The OCR engine ran and failed (a decode error, not a missing binary)."""

    def __init__(self, image_path: str, reason: str, cause: BaseException | None = None) -> None:
        self.image_path = str(image_path)
        self.reason = reason
        super().__init__(
            f"OCR of {self.image_path!r} failed: {reason}. "
            "The image exists but could not be decoded; retake the capture."
        )
        if cause is not None:
            self.__cause__ = cause


class ScreenImageNotFoundError(ScreenError):
    """The image handed to the OCR path does not exist.

    Names the exact path that was looked for, because a typo'd temp path is the
    usual cause and an empty string would hide it.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        super().__init__(
            f"Screen image not found at {self.path!r}: nothing to read. "
            "Capture the screen first with capture_screen() / read_screen()."
        )


class ScreenCaptureUnavailableError(ScreenError):
    """No capture mechanism on this machine could produce a PNG.

    Carries the list of mechanisms that were tried (with the reason each one
    failed) and the packages that would provide one, so the fix is a command
    rather than a guess.
    """

    def __init__(self, attempts: Sequence[tuple[str, str]] = (), fixes: Sequence[str] = ()) -> None:
        self.attempts = list(attempts)
        self.fixes = list(fixes)
        detail = "Screen capture unavailable: every capture mechanism failed."
        if self.attempts:
            detail += "\nTried:\n" + "\n".join(f"    - {name}: {why}" for name, why in self.attempts)
        if self.fixes:
            detail += "\nFix (any one of these):\n" + "\n".join(f"    {f}" for f in self.fixes)
        else:
            detail += (
                "\nFix: install one of `scrot`, `imagemagick` (provides `import`) or "
                "`maim`, and make sure DISPLAY is set for the session Claire runs in."
            )
        super().__init__(detail)


# --------------------------------------------------------------------------- #
# Distro hints: the fix has to be the package name this machine actually uses.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DistroHint:
    """Which package manager this distro uses, and how to install a package."""

    distro_id: str
    family: str
    apt_like: str
    command_template: str

    def command(self, package: str) -> str:
        return self.command_template.format(package=package)


#: id / id_like prefix -> (family, package template, command template).
_FAMILIES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("debian", "ubuntu", "linuxmint", "mint", "pop", "elementary", "raspbian", "kali"), "debian", "sudo apt-get install -y {package}"),
    (("fedora", "rhel", "centos", "rocky", "almalinux", "ol"), "redhat", "sudo dnf install -y {package}"),
    (("arch", "manjaro", "endeavouros", "cachyos"), "arch", "sudo pacman -S --noconfirm {package}"),
    (("opensuse", "suse", "sles"), "suse", "sudo zypper install -y {package}"),
    (("alpine",), "alpine", "sudo apk add {package}"),
)

#: Package name per family for each thing we may need to install.
_PACKAGES: dict[str, dict[str, str]] = {
    "tesseract": {
        "debian": "tesseract-ocr",
        "redhat": "tesseract",
        "arch": "tesseract",
        "suse": "tesseract-ocr",
        "alpine": "tesseract-ocr",
        "unknown": "tesseract-ocr",
    },
    "pillow": {
        "debian": "python3-pil",
        "redhat": "python3-pillow",
        "arch": "python-pillow",
        "suse": "python3-Pillow",
        "alpine": "py3-pillow",
        "unknown": "python3-pil",
    },
    "imagemagick": {
        "debian": "imagemagick",
        "redhat": "ImageMagick",
        "arch": "imagemagick",
        "suse": "ImageMagick",
        "alpine": "imagemagick",
        "unknown": "imagemagick",
    },
    "scrot": {
        "debian": "scrot",
        "redhat": "scrot",
        "arch": "scrot",
        "suse": "scrot",
        "alpine": "scrot",
        "unknown": "scrot",
    },
    "maim": {
        "debian": "maim",
        "redhat": "maim",
        "arch": "maim",
        "suse": "maim",
        "alpine": "maim",
        "unknown": "maim",
    },
}


def distro_hint() -> DistroHint:
    """Read ``/etc/os-release`` and describe this machine's package manager.

    Never raises: on an unreadable or unknown distro it returns the ``unknown``
    family, whose command names both apt and dnf so the hint is still usable.
    """

    distro_id = ""
    id_like = ""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        value = value.strip().strip('"')
        if key.strip() == "ID":
            distro_id = value.lower()
        elif key.strip() == "ID_LIKE":
            id_like = value.lower()

    haystack = f"{distro_id} {id_like}"
    for prefixes, family, template in _FAMILIES:
        if any(prefix in haystack for prefix in prefixes):
            return DistroHint(distro_id or family, family, template.split()[1], template)
    return DistroHint(
        distro_id or "unknown",
        "unknown",
        "apt-get",
        "sudo apt-get install -y {package}   # or: sudo dnf install -y {package}",
    )


def package_for(thing: str, hint: DistroHint | None = None) -> str:
    """Return the installable package name for ``thing`` on this distro."""

    hint = hint or distro_hint()
    table = _PACKAGES.get(thing, {})
    return table.get(hint.family) or table.get("unknown") or thing


# --------------------------------------------------------------------------- #
# The OCR seam. Anything satisfying OcrEngine can stand in for tesseract.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OcrWord:
    """One recognised word and where it sits on the image, in pixels.

    ``line_number`` is tesseract's own line index, so words on one visual line
    group together even when the layout is multi-column.
    """

    text: str
    left: int
    top: int
    width: int
    height: int
    line_number: int = 0

    @property
    def centre(self) -> tuple[float, float]:
        return (self.left + self.width / 2.0, self.top + self.height / 2.0)


@runtime_checkable
class OcrEngine(Protocol):
    """Minimal seam over an OCR engine.

    ``image_to_text`` is all a caller must provide. ``image_to_words`` is
    optional: when present it is used to place a phrase on the image instead of
    merely on a line of text.
    """

    name: str

    def image_to_text(self, image_path: str | Path) -> str:  # pragma: no cover - protocol
        ...


class TesseractOcrEngine:
    """The real engine: ``pytesseract`` driving the ``tesseract`` binary.

    ``which`` is injectable so the missing-binary path can be exercised without
    uninstalling anything.
    """

    name = "tesseract"

    def __init__(
        self,
        binary: str = TESSERACT_BINARY,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.binary = binary
        self._which = which or shutil.which

    # -- availability ------------------------------------------------------- #

    def binary_path(self) -> str | None:
        return self._which(self.binary)

    def module_available(self) -> bool:
        try:
            import pytesseract  # noqa: F401
        except Exception:
            return False
        return True

    def available(self) -> bool:
        return self.binary_path() is not None and self.module_available()

    def missing_binary_error(self, cause: BaseException | None = None) -> OcrEngineMissingError:
        hint = distro_hint()
        package = package_for("tesseract", hint)
        return OcrEngineMissingError(
            binary=self.binary,
            package=package,
            command=hint.command(package),
            distro=hint.distro_id,
            cause=cause,
        )

    def _require(self, cause: BaseException | None = None) -> Any:
        """Return the ``pytesseract`` module, or raise a named error."""

        try:
            import pytesseract
        except Exception as exc:  # pragma: no cover - pytesseract is installed here
            raise OcrModuleMissingError("pytesseract", "pytesseract", cause=exc) from exc
        if self.binary_path() is None:
            raise self.missing_binary_error(cause)
        return pytesseract

    # -- reading ------------------------------------------------------------ #

    def image_to_text(self, image_path: str | Path) -> str:
        module = self._require()
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover - Pillow is installed here
            raise OcrModuleMissingError("PIL", "pillow", cause=exc) from exc
        try:
            with Image.open(image_path) as image:
                return module.image_to_string(image)
        except OcrEngineMissingError:
            raise
        except Exception as exc:
            # pytesseract raises its own not-found error when the binary vanished
            # between the check above and the call.
            if type(exc).__name__ == "TesseractNotFoundError":
                raise self.missing_binary_error(exc) from exc
            raise OcrFailedError(str(image_path), f"{type(exc).__name__}: {exc}", cause=exc) from exc

    def image_to_words(self, image_path: str | Path) -> list[OcrWord]:
        module = self._require()
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover - Pillow is installed here
            raise OcrModuleMissingError("PIL", "pillow", cause=exc) from exc
        try:
            with Image.open(image_path) as image:
                data = module.image_to_data(image, output_type=module.Output.DICT)
        except OcrEngineMissingError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "TesseractNotFoundError":
                raise self.missing_binary_error(exc) from exc
            raise OcrFailedError(str(image_path), f"{type(exc).__name__}: {exc}", cause=exc) from exc

        words: list[OcrWord] = []
        for index, raw in enumerate(data.get("text", [])):
            text = (raw or "").strip()
            if not text:
                continue
            words.append(
                OcrWord(
                    text=text,
                    left=int(data["left"][index]),
                    top=int(data["top"][index]),
                    width=int(data["width"][index]),
                    height=int(data["height"][index]),
                    line_number=int(data["line_num"][index]),
                )
            )
        return words


@dataclass(frozen=True)
class OcrDiagnosis:
    """A structured verdict on whether OCR can run here, and the fix if not.

    Returned by :func:`diagnose`. ``available`` is the single boolean a caller
    should branch on; ``reason`` is the same sentence a human would need.
    """

    engine: str
    binary: str
    binary_path: str | None
    binary_present: bool
    python_module: str
    python_module_present: bool
    available: bool
    distro: str
    distro_family: str
    package: str
    fix_command: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "binary": self.binary,
            "binary_path": self.binary_path,
            "binary_present": self.binary_present,
            "python_module": self.python_module,
            "python_module_present": self.python_module_present,
            "available": self.available,
            "distro": self.distro,
            "distro_family": self.distro_family,
            "package": self.package,
            "fix_command": self.fix_command,
            "reason": self.reason,
        }

    def as_error(self) -> OcrEngineMissingError:
        """The named error this verdict corresponds to."""

        return OcrEngineMissingError(
            binary=self.binary,
            package=self.package,
            command=self.fix_command,
            distro=self.distro,
        )


def _diagnose_engine(engine: Any) -> OcrDiagnosis:
    """Probe ``engine`` for a structured verdict.

    Three kinds of engine are handled honestly: one that reports its own
    ``available()``, one that shells out to a binary (probe ``binary_path`` and
    the Python wrapper), and a stand-in that has no binary notion at all - which
    cannot be probed, so it is taken at face value rather than failed against
    tesseract's binary.
    """

    hint = distro_hint()
    binary = getattr(engine, "binary", "") or getattr(engine, "name", TESSERACT_BINARY)
    module_name = "pytesseract"
    package = package_for("tesseract", hint)
    fix_command = hint.command(package)

    own_available = getattr(engine, "available", None)
    binary_path_fn = getattr(engine, "binary_path", None)
    module_available_fn = getattr(engine, "module_available", None)

    if callable(own_available) and not callable(binary_path_fn):
        # An engine that answers for itself (a fake, or another backend).
        available = bool(own_available())
        binary_path = shutil.which(binary) if binary else None
        module_present = available
        reason = (
            f"OCR engine {getattr(engine, 'name', binary)!r} reports itself available."
            if available
            else f"OCR engine {getattr(engine, 'name', binary)!r} reports itself unavailable. "
            f"Fix: {fix_command} (or install the engine {binary!r} names)"
        )
        return OcrDiagnosis(
            engine=getattr(engine, "name", binary),
            binary=binary,
            binary_path=binary_path,
            binary_present=binary_path is not None,
            python_module=module_name,
            python_module_present=module_present,
            available=available,
            distro=hint.distro_id,
            distro_family=hint.family,
            package=package,
            fix_command=fix_command,
            reason=reason,
        )

    if not callable(binary_path_fn) and not callable(module_available_fn) and not hasattr(engine, "binary"):
        # A pure stand-in with no binary notion (e.g. a test double).
        return OcrDiagnosis(
            engine=getattr(engine, "name", "injected"),
            binary="",
            binary_path=None,
            binary_present=False,
            python_module=module_name,
            python_module_present=False,
            available=True,
            distro=hint.distro_id,
            distro_family=hint.family,
            package=package,
            fix_command=fix_command,
            reason=(
                f"OCR engine {getattr(engine, 'name', 'injected')!r} was injected and exposes no "
                "binary to probe; it is assumed to work."
            ),
        )

    if callable(binary_path_fn):
        binary_path = binary_path_fn()
    else:
        binary_path = shutil.which(binary)

    if callable(module_available_fn):
        module_present = bool(module_available_fn())
    else:  # pragma: no cover - engines with a binary but no module checker
        try:
            import pytesseract  # noqa: F401

            module_present = True
        except Exception:
            module_present = False

    available = binary_path is not None and module_present

    if available:
        reason = f"OCR ready: {binary!r} found at {binary_path!r} and {module_name} importable."
    elif binary_path is None and not module_present:
        reason = (
            f"The {binary!r} binary is not installed and {module_name} is not importable. "
            f"Fix: {fix_command}"
        )
    elif binary_path is None:
        reason = (
            f"The {binary!r} binary is not installed ({module_name} is importable but is only "
            f"a wrapper around it). Fix: {fix_command}"
        )
    else:
        reason = (
            f"{module_name} is not importable (the {binary!r} binary is present). "
            f"Fix: pip install pytesseract"
        )

    return OcrDiagnosis(
        engine=getattr(engine, "name", "tesseract"),
        binary=binary,
        binary_path=binary_path,
        binary_present=binary_path is not None,
        python_module=module_name,
        python_module_present=module_present,
        available=available,
        distro=hint.distro_id,
        distro_family=hint.family,
        package=package,
        fix_command=fix_command,
        reason=reason,
    )


def diagnose(engine: Any | None = None) -> OcrDiagnosis:
    """Return a structured verdict on OCR availability here.

    Cheap and side-effect free: it looks for the binary and tries the import,
    it does not read an image. Call it before promising the user any text.
    """

    return _diagnose_engine(engine if engine is not None else TesseractOcrEngine())


def available(engine: Any | None = None) -> bool:
    """True when OCR can actually run (binary present *and* wrapper importable)."""

    return diagnose(engine).available


def ocr_image(image_path: str | os.PathLike[str], engine: Any | None = None) -> str:
    """Read the text off ``image_path``, returning ``''`` only for a wordless image.

    A missing file raises :class:`ScreenImageNotFoundError` naming the path; a
    missing ``tesseract`` binary raises :class:`OcrEngineMissingError` naming the
    binary and the package that installs it. Neither returns ``''``.
    """

    path = Path(image_path)
    if not path.exists():
        raise ScreenImageNotFoundError(path)
    if not path.is_file():
        raise ScreenImageNotFoundError(path)
    engine = engine if engine is not None else TesseractOcrEngine()
    text = engine.image_to_text(path)
    return text if text is not None else ""


# --------------------------------------------------------------------------- #
# Capture. Reports which mechanism actually produced the file.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CaptureResult:
    """A PNG on disk, and the name of the mechanism that produced it."""

    path: Path
    byte_size: int
    mechanism: str

    def __post_init__(self) -> None:
        if self.byte_size <= 0:
            raise ScreenCaptureUnavailableError(
                [(self.mechanism, "produced a zero-byte PNG")],
            )


def default_capture_path() -> Path:
    """A timestamped path under the system temp dir; the file is kept, not deleted."""

    directory = Path(tempfile.gettempdir()) / "claire_senses"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"screen_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.png"


def _upstream_macos_binary() -> str | None:
    """Reuse upstream's own mechanism discovery for the macOS branch.

    ``claire.tools.builtin.screenshot`` is imported (never edited) so the Darwin
    route uses the same binary it does. Its module-level logic is not reusable
    beyond that: it invokes ``screencapture -i`` (interactive region select),
    consumes the OCR text and deletes the PNG, so it cannot hand back a capture
    path - which is precisely why this module exists. The upstream module also
    only recognises macOS, which is why it returns ``''`` on this Linux machine.
    """

    try:
        from claire.tools.builtin import screenshot as upstream
    except Exception:  # pragma: no cover - upstream import is best-effort
        try:
            from ..tools.builtin import screenshot as upstream  # type: ignore
        except Exception:
            return None
    return shutil.which("screencapture")


def _capture_with_pillow(destination: Path) -> str:
    """Pillow's own X11 grab. The Python-native route; no external binary."""

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise ScreenCaptureUnavailableError([("pillow-imagegrab", "DISPLAY is not set")])
    try:
        from PIL import ImageGrab
    except Exception as exc:
        raise ScreenCaptureUnavailableError(
            [("pillow-imagegrab", f"Pillow ImageGrab not importable: {exc}")],
            fixes=["pip install pillow"],
        ) from exc
    try:
        image = ImageGrab.grab()
        image.save(destination, "PNG")
    except Exception as exc:
        raise ScreenCaptureUnavailableError(
            [("pillow-imagegrab", f"{type(exc).__name__}: {exc}")],
        ) from exc
    return "pillow-imagegrab"


def _capture_with_tool(name: str, argv: Sequence[str], destination: Path) -> str:
    binary = argv[0]
    path = shutil.which(binary)
    if path is None:
        raise ScreenCaptureUnavailableError([(name, f"{binary!r} is not on PATH")])
    command = [path, *argv[1:]]
    command = [str(destination) if arg == "{out}" else arg for arg in command]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=30)
    except Exception as exc:
        raise ScreenCaptureUnavailableError(
            [(name, f"{type(exc).__name__}: {exc}")],
        ) from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
        raise ScreenCaptureUnavailableError(
            [(name, f"exit {completed.returncode}: {stderr[:200] or 'no stderr'}")],
        )
    if not destination.exists():
        raise ScreenCaptureUnavailableError([(name, "exited 0 but wrote no file")])
    return name


def _capture_mechanisms() -> list[tuple[str, Callable[[Path], str]]]:
    """Capture routes in preference order for this platform.

    On Linux the Python-native Pillow grab goes first (it is the only route that
    needs no extra package on this machine); the CLI tools are the fallbacks,
    including Wayland's ``grim`` and KDE's non-interactive ``spectacle``.
    """

    mechanisms: list[tuple[str, Callable[[Path], str]]] = [("pillow-imagegrab", _capture_with_pillow)]

    if sys_platform() == "darwin":  # pragma: no cover - macOS only
        if _upstream_macos_binary():
            mechanisms.append(
                (
                    "screencapture (upstream screenshot mechanism)",
                    lambda out: _capture_with_tool("screencapture", ["screencapture", "-x", "{out}"], out),
                )
            )
    else:
        mechanisms.extend(
            [
                ("grim", lambda out: _capture_with_tool("grim", ["grim", "{out}"], out)),
                (
                    "spectacle",
                    lambda out: _capture_with_tool(
                        "spectacle", ["spectacle", "-b", "-n", "-o", "{out}"], out
                    ),
                ),
                (
                    "gnome-screenshot",
                    lambda out: _capture_with_tool(
                        "gnome-screenshot", ["gnome-screenshot", "-f", "{out}"], out
                    ),
                ),
                (
                    "xfce4-screenshooter",
                    lambda out: _capture_with_tool(
                        "xfce4-screenshooter", ["xfce4-screenshooter", "-f", "-s", "{out}"], out
                    ),
                ),
                (
                    "imagemagick-import",
                    lambda out: _capture_with_tool("imagemagick-import", ["import", "-window", "root", "{out}"], out),
                ),
                ("maim", lambda out: _capture_with_tool("maim", ["maim", "{out}"], out)),
                ("scrot", lambda out: _capture_with_tool("scrot", ["scrot", "-o", "{out}"], out)),
            ]
        )
    return mechanisms


def sys_platform() -> str:
    """Indirection so tests can pretend to be on another OS."""

    import sys

    return sys.platform


def capture_fix_hints() -> list[str]:
    """The exact install commands that would give this machine a fallback capture."""

    hint = distro_hint()
    return [
        hint.command(package_for("imagemagick", hint)) + "   # provides `import`",
        hint.command(package_for("scrot", hint)) + "   # provides `scrot`",
        hint.command(package_for("maim", hint)) + "   # provides `maim`",
        hint.command(package_for("pillow", hint)) + "   # Python-native ImageGrab",
    ]


def capture_screen(
    destination: str | os.PathLike[str] | None = None,
    capture_fn: Callable[[Path], str] | None = None,
) -> CaptureResult:
    """Capture the screen to a PNG and report which mechanism worked.

    ``capture_fn`` is the injectable seam: tests pass a function that writes a
    small PNG, so no real screen is ever read. Raises
    :class:`ScreenCaptureUnavailableError` listing every mechanism tried and the
    commands that would install one.
    """

    out = Path(destination) if destination is not None else default_capture_path()
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)

    if capture_fn is not None:
        mechanism = capture_fn(out)
        if not out.exists():
            raise ScreenCaptureUnavailableError(
                [(str(mechanism), "the injected capture_fn wrote no file")],
            )
        return CaptureResult(path=out, byte_size=out.stat().st_size, mechanism=str(mechanism))

    attempts: list[tuple[str, str]] = []
    for name, fn in _capture_mechanisms():
        try:
            mechanism = fn(out)
        except ScreenCaptureUnavailableError as exc:
            attempts.extend(exc.attempts or [(name, str(exc))])
            continue
        except Exception as exc:  # pragma: no cover - defensive
            attempts.append((name, f"{type(exc).__name__}: {exc}"))
            continue
        if out.exists() and out.stat().st_size > 0:
            return CaptureResult(path=out, byte_size=out.stat().st_size, mechanism=mechanism)
        attempts.append((name, "exited without writing a non-empty PNG"))
    raise ScreenCaptureUnavailableError(attempts, capture_fix_hints())


# --------------------------------------------------------------------------- #
# read_screen: capture + OCR, with the mechanism report attached.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScreenReadResult:
    """Everything that happened during one read of the screen.

    ``status`` is the field that keeps a wordless screen apart from a read that
    never happened: ``ocr_empty`` means OCR ran and found nothing,
    ``ocr_not_run`` means it was never asked to run (``read_screen(ocr=False)``).
    """

    text: str
    capture_path: Path
    byte_size: int
    capture_mechanism: str
    ocr_mechanism: str
    ocr_ran: bool
    status: str
    diagnosis: OcrDiagnosis
    note: str = ""

    @property
    def has_text(self) -> bool:
        """True only when OCR ran and produced at least one non-space character."""

        return self.ocr_ran and bool(self.text.strip())

    @property
    def is_blank_screen(self) -> bool:
        """True when OCR ran on the capture and the screen held no words."""

        return self.ocr_ran and not self.text.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "capture_path": str(self.capture_path),
            "byte_size": self.byte_size,
            "capture_mechanism": self.capture_mechanism,
            "ocr_mechanism": self.ocr_mechanism,
            "ocr_ran": self.ocr_ran,
            "status": self.status,
            "has_text": self.has_text,
            "note": self.note,
        }


def read_screen(
    destination: str | os.PathLike[str] | None = None,
    engine: Any | None = None,
    capture_fn: Callable[[Path], str] | None = None,
    ocr: bool = True,
) -> ScreenReadResult:
    """Capture the screen and read the text on it.

    Raises :class:`OcrEngineMissingError` when OCR is requested but the engine
    cannot run (the real case on this machine) - it never returns a result whose
    empty ``text`` could be mistaken for a screen with no words. With
    ``ocr=False`` the capture is still made and ``status`` is ``ocr_not_run``.
    """

    engine = engine if engine is not None else TesseractOcrEngine()
    capture = capture_screen(destination, capture_fn=capture_fn)
    verdict = diagnose(engine)

    if not ocr:
        return ScreenReadResult(
            text="",
            capture_path=capture.path,
            byte_size=capture.byte_size,
            capture_mechanism=capture.mechanism,
            ocr_mechanism="none",
            ocr_ran=False,
            status=STATUS_NOT_RUN,
            diagnosis=verdict,
            note="OCR was not requested (ocr=False); the capture was taken but no words were read.",
        )

    if not verdict.available:
        raise verdict.as_error()

    text = ocr_image(capture.path, engine=engine)
    has_words = bool(text.strip())
    return ScreenReadResult(
        text=text,
        capture_path=capture.path,
        byte_size=capture.byte_size,
        capture_mechanism=capture.mechanism,
        ocr_mechanism=verdict.engine,
        ocr_ran=True,
        status=STATUS_TEXT if has_words else STATUS_EMPTY,
        diagnosis=verdict,
        note="" if has_words else "OCR ran and found no words on the screen (not an error).",
    )


# --------------------------------------------------------------------------- #
# Finding a phrase: the actual use case ("find the button").
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PhraseLocation:
    """A rough location for a phrase: which line, and which part of the image."""

    phrase: str
    found: bool
    line_number: int | None = None
    column: int | None = None
    line_text: str | None = None
    region: str | None = None
    quarter: str | None = None
    match_count: int = 0
    image_size: tuple[int, int] | None = None
    source: str = "ocr_text_only"

    def describe(self) -> str:
        """One spoken-style sentence locating the phrase."""

        if not self.found:
            return f"{self.phrase!r} was not found on the screen."
        where = f"line {self.line_number}"
        if self.region:
            where += f", in the {self.region} of the screen"
        if self.quarter:
            where += f" ({self.quarter})"
        return f"{self.phrase!r} is at {where}: {self.line_text!r}"


#: Vertical bands used when only text (no pixel boxes) is available.
_VERTICAL_BANDS = ("top", "upper-middle", "lower-middle", "bottom")


def vertical_band(index: int, total: int) -> str:
    """Name the quarter of the page a 0-based line falls in."""

    if total <= 0:
        return _VERTICAL_BANDS[0]
    fraction = (index + 0.5) / total
    position = min(3, int(fraction * 4))
    return _VERTICAL_BANDS[position]


def _quadrant(x: float, y: float, width: int, height: int) -> tuple[str, str]:
    vertical = _VERTICAL_BANDS[min(3, int((y / max(height, 1)) * 4))]
    horizontal = "left" if x < width / 2.0 else "right"
    return vertical, f"{'top' if (y / max(height, 1)) < 0.5 else 'bottom'}-{horizontal}"


def find_phrase(
    text: str,
    phrase: str,
    *,
    case_sensitive: bool = False,
    image_size: tuple[int, int] | None = None,
    words: Sequence[OcrWord] | None = None,
) -> PhraseLocation:
    """Locate ``phrase`` in OCR ``text``, reporting line and rough image region.

    When ``words`` (with pixel boxes) and ``image_size`` are supplied the region
    is a true quadrant of the image; otherwise it is the vertical quarter of the
    text, which is all that can honestly be said from text alone.
    """

    needle = phrase if case_sensitive else phrase.lower()
    needle = needle.strip()
    if not needle:
        raise ValueError("find_phrase needs a non-empty phrase to look for")

    lines = text.splitlines()
    haystack: list[str] = lines if case_sensitive else [line.lower() for line in lines]

    first_line: int | None = None
    first_column: int | None = None
    first_line_text: str | None = None
    match_count = 0
    for index, line in enumerate(haystack):
        column = line.find(needle)
        if column < 0:
            continue
        match_count += line.count(needle)
        if first_line is None:
            first_line, first_column, first_line_text = index, column, lines[index]

    if first_line is None:
        return PhraseLocation(
            phrase=phrase,
            found=False,
            match_count=0,
            image_size=image_size,
            source="ocr_text_only",
        )

    region = vertical_band(first_line, len(lines) or 1)
    quarter: str | None = None
    source = "ocr_text_only"

    if words and image_size:
        width, height = image_size
        for word in words:
            word_text = word.text if case_sensitive else word.text.lower()
            if needle.replace(" ", "") and needle.replace(" ", "") in word_text:
                x, y = word.centre
                region, quarter = _quadrant(x, y, width, height)
                source = "ocr_words"
                break

    return PhraseLocation(
        phrase=phrase,
        found=True,
        line_number=first_line + 1,
        column=first_column,
        line_text=first_line_text,
        region=region,
        quarter=quarter,
        match_count=match_count,
        image_size=image_size,
        source=source,
    )


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def find_phrase_on_screen(
    phrase: str,
    destination: str | os.PathLike[str] | None = None,
    engine: Any | None = None,
    capture_fn: Callable[[Path], str] | None = None,
    case_sensitive: bool = False,
) -> PhraseLocation:
    """Capture the screen, read it, and say where ``phrase`` is.

    Uses the engine's word boxes when they are offered, so the answer can name a
    quadrant of the image rather than just a line of text.
    """

    read = read_screen(destination=destination, engine=engine, capture_fn=capture_fn)
    words: Sequence[OcrWord] | None = None
    word_reader = getattr(engine, "image_to_words", None)
    if callable(word_reader):
        try:
            words = word_reader(read.capture_path)
        except ScreenError:
            words = None
    return find_phrase(
        read.text,
        phrase,
        case_sensitive=case_sensitive,
        image_size=_image_size(read.capture_path),
        words=words,
    )
