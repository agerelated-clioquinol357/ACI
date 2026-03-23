"""
OpenClaw 2.0 ACI Framework - T2 Web Vision Fallback.

Wraps desktop bridge's FastOCR and VLMIdentifier for web pages where the DOM
is inaccessible (canvas, WebGL, complex iframes, etc.).  The wrapper converts
DetectedElement results into UIDNode instances with ``vc_`` prefixed UIDs so
they can be seamlessly merged into the web bridge perception pipeline.

FastOCR and VLMIdentifier may not be importable on all systems (they depend
on Windows OCR / Tesseract / PIL / httpx), so imports are lazy and the
fallback degrades gracefully to an empty result list.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from core.detection_tier import DetectedElement, TierResult
from core.models.schemas import UIDNode

logger = logging.getLogger(__name__)

_VISION_ENABLED = os.environ.get("OPENCLAW_WEB_VISION_FALLBACK", "1") != "0"

# Maximum number of vision-detected elements to return per extraction.
_T2_MAX = 50

# Lazy-loaded tier classes — populated by _ensure_vision_imports().
_FastOCR = None
_VLMIdentifier = None


def _ensure_vision_imports() -> bool:
    """Attempt to import FastOCR and VLMIdentifier from the desktop bridge.

    Returns True if at least one backend is importable.
    """
    global _FastOCR, _VLMIdentifier
    if _FastOCR is not None or _VLMIdentifier is not None:
        return True
    try:
        from bridges.desktop_bridge.fast_ocr import FastOCR
        _FastOCR = FastOCR
    except ImportError:
        pass
    try:
        from bridges.desktop_bridge.vlm_identifier import VLMIdentifier
        _VLMIdentifier = VLMIdentifier
    except ImportError:
        pass
    return _FastOCR is not None or _VLMIdentifier is not None


def _detected_to_uid_node(det: DetectedElement, index: int) -> UIDNode:
    """Convert a DetectedElement to a UIDNode with a ``vc_`` prefixed UID."""
    return UIDNode(
        uid=f"vc_{index}",
        tag=det.tag or "unknown",
        role=det.extra.get("role", "") if det.extra else "",
        text=(det.label or "")[:200],
        bbox=det.bbox,
        interactable=det.interactable,
        attributes={"confidence": str(det.confidence), "source": "vision"},
        tier="vision",
    )


class WebVisionFallback:
    """T2 vision fallback for web pages with inaccessible DOM.

    Usage::

        fb = WebVisionFallback()
        nodes = await fb.extract(screenshot_bytes=png_bytes)
    """

    def __init__(self) -> None:
        self._available = _ensure_vision_imports() if _VISION_ENABLED else False

    async def extract(self, screenshot_bytes: bytes) -> list[UIDNode]:
        """Run vision detection on a screenshot and return UIDNode results.

        Args:
            screenshot_bytes: Full-page screenshot as PNG bytes.

        Returns:
            List of UIDNode instances (up to ``_T2_MAX``), or empty list on
            failure or if vision modules are unavailable.
        """
        if not self._available:
            logger.warning("WebVisionFallback: vision modules not available")
            return []

        try:
            result = await self._run_detection(screenshot_bytes)
        except Exception as exc:
            logger.error("WebVisionFallback: detection failed: %s", exc)
            return []

        nodes = [
            _detected_to_uid_node(det, i)
            for i, det in enumerate(result.elements[:_T2_MAX])
        ]
        logger.info("WebVisionFallback: detected %d elements", len(nodes))
        return nodes

    async def _run_detection(self, screenshot_bytes: bytes) -> TierResult:
        """Delegate to the first available detection tier.

        Prefers FastOCR (faster, lower tier) over VLMIdentifier.
        """
        if _FastOCR is not None:
            ocr = _FastOCR()
            return await asyncio.to_thread(ocr.detect, screenshot_bytes, [])
        if _VLMIdentifier is not None:
            vlm = _VLMIdentifier()
            return await asyncio.to_thread(vlm.detect, screenshot_bytes, [])
        return TierResult(elements=[], source_name="none", elapsed_ms=0.0)
