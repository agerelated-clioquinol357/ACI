"""
OpenClaw 2.0 ACI Framework - Vision Fallback (T2 Waterfall + Annotation).

When the structured UIA tree fails to locate a target element, the desktop
bridge falls back to vision-based detection:

* **T2 (fast_match):** OpenCV template matching against a muscle-memory cache
  of previously seen UI regions.  This is fast (~10ms) but only works if we
  have seen the exact visual pattern before.

The primary detection pipeline now uses the four-tier waterfall
(CursorProbe → FastOCR → ContourDetector → VLMIdentifier) in worker.py.
This module provides the T2 cache lookup, screenshot annotation for the
annotated "打靶图", and legacy find_target waterfall.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import Path
from typing import Optional

from memory_core.muscle_memory import MuscleMemoryStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependencies (graceful degradation)
# ---------------------------------------------------------------------------

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    _CV2_AVAILABLE = False
    logger.info(
        "vision_fallback: OpenCV not available. T2 fast_match disabled. "
        "Install with: pip install opencv-python-headless numpy"
    )

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    Image = None  # type: ignore[assignment,misc]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False
    logger.info(
        "vision_fallback: Pillow not available. annotate_screenshot disabled. "
        "Install with: pip install Pillow"
    )

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False
    logger.info(
        "vision_fallback: httpx not available. T3 vlm_analyze disabled. "
        "Install with: pip install httpx"
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# VLM endpoint URL (configurable via environment).
_VLM_ENDPOINT = os.environ.get(
    "OPENCLAW_VLM_ENDPOINT",
    "http://127.0.0.1:8100/api/v1/analyze",
)

# Timeout for VLM HTTP requests.
_VLM_TIMEOUT_S = float(os.environ.get(
    "OPENCLAW_VLM_TIMEOUT", "60.0",
))

# Unified muscle memory store (replaces local _cache_key/_load/_save helpers).
_muscle_memory = MuscleMemoryStore()


# ---------------------------------------------------------------------------
# T2: Fast template matching (delegates to MuscleMemoryStore)
# ---------------------------------------------------------------------------

def fast_match(
    screenshot_bytes: bytes,
    semantic_description: str,
) -> Optional[tuple[int, int]]:
    """Attempt to locate a UI element using cached template matching.

    Delegates to the unified :class:`MuscleMemoryStore`.

    Args:
        screenshot_bytes: Full screenshot as PNG bytes.
        semantic_description: Natural language description of the target.

    Returns:
        ``(x, y)`` center coordinates of the best match, or ``None`` if
        no template is cached or the match confidence is below threshold.
    """
    return _muscle_memory.fast_match(screenshot_bytes, semantic_description)


# ---------------------------------------------------------------------------
# T3: VLM analysis (remote endpoint, legacy)
# ---------------------------------------------------------------------------

async def vlm_analyze(
    screenshot_bytes: bytes,
    description: Optional[str] = None,
) -> Optional[list[dict]]:
    """Send a screenshot to the remote VLM endpoint for element detection.

    Args:
        screenshot_bytes: Full screenshot as PNG bytes.
        description: Optional semantic description to guide detection.

    Returns:
        List of bounding box dicts ``{"label", "bbox": [x, y, w, h], "confidence"}``,
        or ``None`` on failure.
    """
    if not _HTTPX_AVAILABLE:
        logger.warning("vision_fallback: T3 unavailable (httpx not installed).")
        return None

    try:
        import base64

        payload: dict = {
            "image": base64.b64encode(screenshot_bytes).decode("ascii"),
            "format": "png",
        }
        if description:
            payload["query"] = description

        async with httpx.AsyncClient(timeout=_VLM_TIMEOUT_S) as client:
            response = await client.post(_VLM_ENDPOINT, json=payload)
            response.raise_for_status()

        data = response.json()

        # The VLM API may return results under various keys; normalise.
        results = data.get("results") or data.get("boxes") or data.get("elements")
        if not isinstance(results, list):
            logger.warning("vision_fallback: T3 response has no results array.")
            return None

        # Normalise each result to a consistent format.
        normalised: list[dict] = []
        for item in results:
            bbox = item.get("bbox") or item.get("box") or item.get("bounding_box")
            if not bbox or len(bbox) < 4:
                continue
            normalised.append({
                "label": item.get("label", item.get("text", "")),
                "bbox": list(bbox[:4]),
                "confidence": float(item.get("confidence", item.get("score", 0.0))),
            })

        logger.info("vision_fallback: T3 returned %d elements.", len(normalised))
        return normalised if normalised else None

    except Exception as exc:
        logger.error("vision_fallback: T3 vlm_analyze error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Full-scene element detection
# ---------------------------------------------------------------------------

async def get_all_elements(
    screenshot_bytes: bytes,
    roi: Optional[tuple[int, int, int, int]] = None,
) -> list[dict]:
    """Detect ALL interactable elements in a screenshot using VLM.

    Args:
        screenshot_bytes: Full screenshot as PNG bytes.
        roi: Optional region of interest as ``(x, y, width, height)``.
            If provided, crops to ROI before analysis and offsets bboxes back.

    Returns:
        List of element dicts ``{"label", "bbox": [x, y, w, h], "confidence"}``.
    """
    offset_x, offset_y = 0, 0
    analysis_bytes = screenshot_bytes

    if roi and _PIL_AVAILABLE:
        try:
            img = Image.open(io.BytesIO(screenshot_bytes))
            rx, ry, rw, rh = roi
            cropped = img.crop((rx, ry, rx + rw, ry + rh))
            buf = io.BytesIO()
            cropped.save(buf, format="PNG")
            analysis_bytes = buf.getvalue()
            offset_x, offset_y = rx, ry
        except Exception as exc:
            logger.warning("vision_fallback: ROI crop failed, using full image: %s", exc)

    elements = await vlm_analyze(analysis_bytes, description=None)
    if not elements:
        return []

    # Offset bboxes back to full-image coordinates if ROI was used.
    if offset_x or offset_y:
        for elem in elements:
            bbox = elem.get("bbox", [0, 0, 0, 0])
            elem["bbox"] = [
                bbox[0] + offset_x,
                bbox[1] + offset_y,
                bbox[2],
                bbox[3],
            ]

    return elements


# ---------------------------------------------------------------------------
# Screenshot annotation
# ---------------------------------------------------------------------------

# High-contrast color palette for bounding boxes (RGB).
_ANNOTATION_COLORS = [
    (255, 0, 0),      # red
    (0, 180, 0),      # green
    (0, 100, 255),    # blue
    (255, 165, 0),    # orange
    (180, 0, 220),    # purple
    (0, 200, 200),    # teal
    (255, 255, 0),    # yellow
    (255, 80, 147),   # pink
    (100, 200, 0),    # lime
    (0, 128, 255),    # sky blue
]

_MAX_ANNOTATED_WIDTH = 1280


def annotate_screenshot(
    screenshot_bytes: bytes,
    elements: list[dict],
) -> bytes:
    """Draw numbered bounding boxes on a screenshot for agent perception.

    Each element gets a colored 2px bounding box and a ``vc_N`` label with
    a contrasting background rectangle at its top-left corner.

    The image is downscaled to ``_MAX_ANNOTATED_WIDTH`` pixels wide (keeping
    aspect ratio) to keep base64 size reasonable.

    Args:
        screenshot_bytes: Original screenshot as PNG bytes.
        elements: List of element dicts with ``"bbox": [x, y, w, h]`` and
            optional ``"label"`` / ``"confidence"`` keys.

    Returns:
        Annotated PNG image as bytes.
    """
    if not _PIL_AVAILABLE:
        logger.warning("vision_fallback: Pillow not available, returning raw screenshot")
        return screenshot_bytes

    try:
        img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Try to load a small truetype font; fall back to default.
        try:
            font = ImageFont.truetype("arial.ttf", 14)
        except (OSError, IOError):
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", 14)
            except (OSError, IOError):
                font = ImageFont.load_default()

        num_colors = len(_ANNOTATION_COLORS)

        for idx, elem in enumerate(elements):
            bbox = elem.get("bbox", [0, 0, 0, 0])
            if len(bbox) < 4:
                continue
            x, y, w, h = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
            color = _ANNOTATION_COLORS[idx % num_colors]
            label_text = f"vc_{idx}"

            # Draw 2px bounding box.
            for offset in range(2):
                draw.rectangle(
                    [x - offset, y - offset, x + w + offset, y + h + offset],
                    outline=color,
                )

            # Draw label background + text.
            text_bbox = draw.textbbox((0, 0), label_text, font=font)
            tw = text_bbox[2] - text_bbox[0]
            th = text_bbox[3] - text_bbox[1]
            label_x = x
            label_y = max(0, y - th - 4)
            draw.rectangle(
                [label_x, label_y, label_x + tw + 6, label_y + th + 4],
                fill=color,
            )
            draw.text(
                (label_x + 3, label_y + 1),
                label_text,
                fill=(255, 255, 255),
                font=font,
            )

        # Downscale if wider than max.
        if img.width > _MAX_ANNOTATED_WIDTH:
            ratio = _MAX_ANNOTATED_WIDTH / img.width
            new_h = int(img.height * ratio)
            img = img.resize((_MAX_ANNOTATED_WIDTH, new_h), Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    except Exception as exc:
        logger.error("vision_fallback: annotate_screenshot failed: %s", exc)
        return screenshot_bytes


# ---------------------------------------------------------------------------
# Waterfall: find_target
# ---------------------------------------------------------------------------

async def find_target(
    screenshot_bytes: bytes,
    description: str,
) -> Optional[tuple[int, int]]:
    """Locate a UI element by description using the T2/T3 waterfall.

    1. **T2** -- Fast template matching against cached patterns.
    2. **T3** -- Remote VLM analysis (if T2 misses or is unavailable).

    On a successful T3 match, the matched region is cropped from the
    screenshot and saved to the T2 cache for future use.

    Args:
        screenshot_bytes: Full screenshot as PNG bytes.
        description: Natural language description of the target element
            (e.g. ``"the blue Submit button"``).

    Returns:
        ``(x, y)`` center coordinates of the target, or ``None`` if the
        element could not be found.
    """
    # --- T2: fast match ---
    t2_result = fast_match(screenshot_bytes, description)
    if t2_result is not None:
        return t2_result

    logger.debug("vision_fallback: T2 miss for %r, escalating to T3.", description)

    # --- T3: VLM analysis ---
    elements = await vlm_analyze(screenshot_bytes, description=description)
    if not elements:
        logger.info("vision_fallback: T3 returned no elements for %r.", description)
        return None

    # Pick the best match by confidence, or the first if no description match.
    best: Optional[dict] = None
    best_score: float = -1.0

    desc_lower = description.lower()
    for elem in elements:
        score = elem.get("confidence", 0.0)
        label = (elem.get("label") or "").lower()

        # Boost score if the label overlaps with the description.
        if desc_lower and label and any(word in label for word in desc_lower.split()):
            score += 0.5

        if score > best_score:
            best_score = score
            best = elem

    if best is None:
        return None

    bbox = best["bbox"]  # [x, y, w, h]
    center_x = int(bbox[0] + bbox[2] / 2)
    center_y = int(bbox[1] + bbox[3] / 2)

    # --- Cache the T3 result for future T2 matches ---
    if _CV2_AVAILABLE:
        try:
            screenshot_arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
            screenshot = cv2.imdecode(screenshot_arr, cv2.IMREAD_COLOR)
            if screenshot is not None:
                x, y, w, h = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
                # Clamp to image bounds.
                y_end = min(y + h, screenshot.shape[0])
                x_end = min(x + w, screenshot.shape[1])
                y_start = max(0, y)
                x_start = max(0, x)

                if y_end > y_start and x_end > x_start:
                    crop = screenshot[y_start:y_end, x_start:x_end]
                    success, png_bytes = cv2.imencode(".png", crop)
                    if success:
                        _muscle_memory.save(description, png_bytes.tobytes())
        except Exception as exc:
            logger.debug("vision_fallback: failed to cache T3 crop: %s", exc)

    logger.info(
        "vision_fallback: T3 match at (%d, %d) confidence=%.3f for %r",
        center_x, center_y, best_score, description,
    )
    return (center_x, center_y)
