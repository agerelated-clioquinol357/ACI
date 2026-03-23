"""
OpenClaw 2.0 ACI Framework - OCR Cross-Validator.

Provides OCR-based text extraction for bounding box regions of a screenshot.
Used to cross-validate element labels — when OCR text differs from a
detected label, the OCR result is treated as ground truth for text content.

Backend: FastOCR module (Windows OCR API + Tesseract fallback).
Replaces the previous RapidOCR implementation for faster, more reliable OCR.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def ocr_crop(image_bytes: bytes, bbox: list[int]) -> Optional[str]:
    """Extract text from a bounding box region of an image via OCR.

    Delegates to the FastOCR module which uses Windows OCR API (primary)
    or Tesseract (fallback).

    Args:
        image_bytes: Full screenshot as PNG bytes.
        bbox: Bounding box as ``[x, y, width, height]``.

    Returns:
        Recognized text string, or ``None`` if OCR fails or is unavailable.
    """
    try:
        from .fast_ocr import ocr_crop as _fast_ocr_crop
        return _fast_ocr_crop(image_bytes, bbox)
    except ImportError:
        logger.debug("ocr_validator: fast_ocr module not available")
        return None
    except Exception as exc:
        logger.debug("ocr_validator: OCR failed for bbox %s: %s", bbox, exc)
        return None
