# Reading Text Off The Screen (R-OCR-1)

Capture the screen to a PNG, read the words on it with pytesseract, and say
where a phrase sits. Linux-only, no network, no LLM.

- File: `src/claire/senses/screen.py`, re-exported by
  `src/claire/senses/__init__.py`.
- Tests: `tests/test_senses_screen.py` (unit-marked).
- Related: `src/claire/tools/builtin/screenshot.py` — the `screenshot` tool now
  delegates to this module's `read_screen()` (lazy import, same pattern as
  `tools/builtin/senses.py`) and converts its named errors into an honest
  `success=False` result. Tests: `tests/test_screenshot_tool.py` (unit-marked).

## Measured facts about this machine (2026-02, Linux Mint 22.3)

| Thing | State | Consequence |
| --- | --- | --- |
| `tesseract` binary | **absent** (`shutil.which` -> `None`) | every OCR call must fail *named*, never `''` |
| `pytesseract` | installed (0.3.13) | importable wrapper with no engine behind it |
| `PIL.ImageGrab.grab()` | **works** (X11, `DISPLAY=:0`, 1920x1080) | the Python-native capture route is the default |
| `scrot` | absent | upstream's CLI assumption is wrong here |
| `spectacle` | present at `/usr/bin/spectacle` | non-interactive fallback (`-b -n -o`) |
| camera | `/dev/video*` absent | not this module's problem; see `senses/` siblings |

## Why this module exists

Upstream's `ScreenshotTool` only knew macOS. It looked for `screencapture`,
found nothing on Linux, skipped its `tesseract` branch, and returned
`ToolExecutionResult` with `ocr_text = ""`. On this machine that was a
*permanent* silent-empty success: the tool reported success and the text was
empty, which is byte-for-byte identical to "the screen has no words on it".
**Fixed:** `screenshot.py` now calls this module's `read_screen()` through a
lazy `_screen_module()` helper; a raised `ScreenError` becomes
`success=False` with the error's own message, and success is never empty.

So the rules are:

1. **No silent empty.** Missing binary -> `OcrEngineMissingError`; missing
   image -> `ScreenImageNotFoundError` naming the path; no capture route ->
   `ScreenCaptureUnavailableError` listing every mechanism tried and the
   command that installs one. None of these return `''`.
2. **No fabricated success.** `read_screen()` reports `capture_mechanism` and
   `ocr_mechanism` by name, so the caller can see *what* produced the text.
3. **Empty is not failure.** A blank screen yields `text=""` with
   `status="ocr_empty"`, `ocr_ran=True`. A capture taken with `ocr=False` yields
   `status="ocr_not_run"`, `ocr_ran=False`. `has_text` is False for both, so the
   status field is what distinguishes them - `is_blank_screen` is True only in
   the first case.

## API

```python
read_screen(engine=None, capture_fn=None, destination=None, ocr=True) -> ScreenReadResult
capture_screen(destination=None, capture_fn=None) -> CaptureResult
ocr_image(path, engine=None) -> str
diagnose(engine=None) -> OcrDiagnosis        # structured verdict
available(engine=None) -> bool               # diagnose().available
find_phrase(text, phrase, ...) -> PhraseLocation
find_phrase_on_screen(phrase, ...) -> PhraseLocation
```

`ScreenReadResult`: `text`, `capture_path`, `byte_size`, `capture_mechanism`,
`ocr_mechanism`, `ocr_ran`, `status`, `diagnosis`, `note`, plus `has_text` /
`is_blank_screen` / `to_dict()`.

## Capture order

1. `pillow-imagegrab` - Python-native, no external binary; requires `DISPLAY`
   or `WAYLAND_DISPLAY`. This is the route that actually works on this machine.
2. On Linux, fallbacks in order: `grim` (Wayland), `spectacle -b -n -o`,
   `gnome-screenshot -f`, `xfce4-screenshooter -f -s`,
   `imagemagick-import` (`import -window root`), `maim`, `scrot`.
3. On Darwin only: `screencapture -x`, reusing upstream's binary discovery via
   a lazy `import` of `claire.tools.builtin.screenshot`. (The upstream tool's
   own capture logic was replaced by a call into *this* module, so nothing
   here depends on it as a mechanism — it cannot produce a capture path.)

Capture files are **kept**, not deleted: `capture_path` is part of the result.

## The OCR seam

`OcrEngine` is a `Protocol` with `name` and `image_to_text(path)`.
`image_to_words(path)` is optional; when an engine offers it,
`find_phrase_on_screen` uses the word boxes to name a quadrant of the image
(`top-left`) rather than only a vertical quarter (`top`). Tests inject a fake
engine and a fake `capture_fn` (which writes a tiny PNG with Pillow), so no real
screen and no real binary is ever needed.

`TesseractOcrEngine(which=...)` takes an injectable binary resolver, so the
missing-binary path is testable without uninstalling tesseract.

## The fix hint is distro-derived

`distro_hint()` reads `/etc/os-release` and maps `ID`/`ID_LIKE` to a family:
debian/ubuntu/mint -> `sudo apt-get install -y tesseract-ocr`,
fedora/rhel -> `sudo dnf install -y tesseract`, arch -> `pacman -S tesseract`,
suse -> `zypper install tesseract-ocr`, alpine -> `apk add tesseract-ocr`.
Unknown distro -> the apt command with the dnf alternative appended.
`OcrEngineMissingError` carries `binary`, `package`, `command`, `distro` as
attributes *and* in the message, so the caller can render it and a human can
copy-paste it.

## Finding a phrase

`find_phrase` is pure text logic and cases-insensitive by default. It reports
1-based `line_number`, `column`, `line_text`, `match_count`, and a `region`
(one of `top`, `upper-middle`, `lower-middle`, `bottom`). With word boxes it
also reports `quarter` (`top-left` ... `bottom-right`) and `source="ocr_words"`.
Not found is a returned `PhraseLocation(found=False)`, not an exception: a
phrase legitimately may not be on screen.
