"""Claire's senses: ways of looking at the world outside the microphone.

Currently one sense: reading text off the screen (R-OCR-1). The public surface
is re-exported here so callers write ``from claire.senses import read_screen``
rather than reaching into the module.
"""

from __future__ import annotations

from .screen import (
    CaptureResult,
    OcrDiagnosis,
    OcrEngine,
    OcrEngineMissingError,
    OcrFailedError,
    OcrModuleMissingError,
    OcrWord,
    PhraseLocation,
    ScreenCaptureUnavailableError,
    ScreenError,
    ScreenImageNotFoundError,
    ScreenReadResult,
    TesseractOcrEngine,
    available,
    capture_screen,
    diagnose,
    find_phrase,
    find_phrase_on_screen,
    ocr_image,
    read_screen,
)

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
